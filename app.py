import os
import time
import inspect
from typing import Callable, List, Tuple, Any, Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

# Unofficial TextNow wrapper (from your wheel)
from pythontextnow import Client

# cache
_CLIENT = None
_CLIENT_TS = 0


def get_client():
    """
    Supports multiple pythontextnow variants:
    - Variant A: Client(email, password)  or  Client("", sid_cookie=...)
    - Variant B: Client(); then .login(email,password) or .login_sid(cookie) (or similar)
    """
    global _CLIENT, _CLIENT_TS
    email = os.environ.get("TEXTNOW_EMAIL")
    password = os.environ.get("TEXTNOW_PASSWORD")
    sid_cookie = os.environ.get("TEXTNOW_SID_COOKIE")

    if not ((email and password) or sid_cookie):
        raise RuntimeError("TEXTNOW_EMAIL (and TEXTNOW_PASSWORD) or TEXTNOW_SID_COOKIE must be set.")

    # reuse existing client if still fresh
    if _CLIENT and (time.time() - _CLIENT_TS < 1800):
        return _CLIENT

    # Try constructor-first
    try:
        if sid_cookie:
            client = Client("", sid_cookie=sid_cookie)
        else:
            client = Client(email, password)
        _CLIENT = client
        _CLIENT_TS = time.time()
        return _CLIENT
    except TypeError:
        # Fallback: no-arg constructor and explicit login method
        client = Client()
        if sid_cookie:
            for m in ("login_sid", "log_in_sid", "login_with_sid", "set_sid"):
                if hasattr(client, m) and callable(getattr(client, m)):
                    getattr(client, m)(sid_cookie)
                    break
            else:
                try:
                    setattr(client, "sid_cookie", sid_cookie)
                except Exception:
                    pass
        else:
            for m in ("login", "log_in", "sign_in"):
                if hasattr(client, m) and callable(getattr(client, m)):
                    getattr(client, m)(email, password)
                    break
            else:
                raise RuntimeError("Could not find a login(email,password) method on pythontextnow.Client")

        _CLIENT = client
        _CLIENT_TS = time.time()
        return _CLIENT


app = FastAPI(title="TextNow SMS Microservice (Unofficial)", version="1.4.0")

# Open CORS for MVP
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SendBody(BaseModel):
    to: str = Field(..., description="10-digit US or E.164 like +18322966196")
    message: str = Field(..., min_length=1, max_length=480)


def normalize_number(raw: str) -> str:
    raw = raw.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if raw.startswith("+") and len(digits) >= 8:
        return raw
    raise HTTPException(status_code=400, detail="Invalid phone number format. Use 10-digit US or E.164.")


def _sig(fn: Callable):
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None


def _try_call(fn: Callable, to: str, message: str, phone_keys=None, msg_keys=None):
    phone_keys = phone_keys or ["to", "phone", "number", "recipient", "contact", "send_to"]
    msg_keys = msg_keys or ["message", "text", "body", "content"]

    # 1) positional (to, message)
    try:
        return fn(to, message)
    except TypeError:
        pass
    # 2) positional swapped (message, to)
    try:
        return fn(message, to)
    except TypeError:
        pass
    # 3) kw combos
    for p in phone_keys:
        for m in msg_keys:
            try:
                return fn(**{p: to, m: message})
            except TypeError:
                continue
    # 4) message-only
    for m in msg_keys:
        try:
            return fn(**{m: message})
        except TypeError:
            continue
    raise RuntimeError("Could not call the discovered send method with known argument patterns.")


def _find_direct_send(client) -> Tuple[str, Callable] | Tuple[None, None]:
    """look for simple top-level sends"""
    for name in ("send_sms", "send_message", "send_text", "text", "send", "sms", "message"):
        if hasattr(client, name) and callable(getattr(client, name)):
            return name, getattr(client, name)
    return None, None


def _find_conversation_maker(client) -> Tuple[str, Callable] | Tuple[None, None]:
    """look for methods that give us a conversation object"""
    candidates = (
        "create_conversation",
        "get_conversation",
        "get_or_create_conversation",
        "open_conversation",
        "conversation_with",
        "start_conversation",
        "ensure_conversation",
        "conversation",  # sometimes a generic accessor
    )
    for name in candidates:
        if hasattr(client, name) and callable(getattr(client, name)):
            return name, getattr(client, name)
    # also check 'conversations' sub-obj for a maker
    if hasattr(client, "conversations"):
        convs = getattr(client, "conversations")
        for name in candidates:
            if hasattr(convs, name) and callable(getattr(convs, name)):
                return f"conversations.{name}", getattr(convs, name)
    return None, None


def _find_conversation_sender(conv_obj) -> Tuple[str, Callable] | Tuple[None, None]:
    """on a conversation object, find its send-ish method"""
    for name in ("send", "send_message", "send_text", "text", "message", "reply"):
        if hasattr(conv_obj, name) and callable(getattr(conv_obj, name)):
            return name, getattr(conv_obj, name)
    # scan all callables for anything that smells like send
    for name, fn in inspect.getmembers(conv_obj, predicate=callable):
        lname = name.lower()
        if lname.startswith("_"):
            continue
        if any(k in lname for k in ("send", "text", "message", "sms")):
            return name, fn
    return None, None


@app.get("/health")
def health():
    return {"ok": True, "service": "textnow", "version": "1.4.0"}


@app.get("/debug")
def debug():
    """quick peek at top-level attributes so we can steer the integration"""
    try:
        client = get_client()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Client init failed: {e}")

    attrs = [n for n in dir(client) if not n.startswith("_")]
    return {"attrs_preview": attrs[:200]}


@app.post("/send")
def send_sms(body: SendBody):
    to = normalize_number(body.to)
    try:
        client = get_client()

        # 1) try direct send on client
        name, fn = _find_direct_send(client)
        if fn:
            _try_call(fn, to, body.message)
            return {"ok": True, "used": f"client.{name}", "to": to, "message_len": len(body.message)}

        # 2) try conversation-based flow
        maker_name, maker = _find_conversation_maker(client)
        if not maker:
            raise RuntimeError("No send method or conversation maker found on client.")

        # Try calling the maker with the number in common ways
        conv = None
        maker_sig = _sig(maker)
        tried = []
        # a) positional
        try:
            conv = maker(to)
        except TypeError as e:
            tried.append(f"{maker_name}(to) -> {e}")
        # b) keyword: number/phone/to
        if conv is None:
            for p in ("to", "phone", "number", "contact", "recipient"):
                try:
                    conv = maker(**{p: to})
                    break
                except TypeError as e:
                    tried.append(f"{maker_name}({p}=to) -> {e}")

        if conv is None:
            raise RuntimeError(f"Found '{maker_name}' but couldn't call it with the phone number. Tried: {tried[:3]}")

        # Find a send on the conversation object
        send_name, send_fn = _find_conversation_sender(conv)
        if not send_fn:
            # Try if conversation has a nested 'messages' or similar
            for subname in ("messages", "sms", "api", "driver"):
                if hasattr(conv, subname):
                    sub = getattr(conv, subname)
                    sname, sfn = _find_conversation_sender(sub)
                    if sfn:
                        send_name, send_fn = f"{subname}.{sname}", sfn
                        break

        if not send_fn:
            raise RuntimeError("Conversation obtained, but no send-ish method on it.")

        # Try calling the conversation's send with sensible patterns
        try:
            # Most conversation senders take just the message
            send_fn(body.message)
        except TypeError:
            # Try generic combos
            _try_call(send_fn, to, body.message)

        return {
            "ok": True,
            "used": f"client.{maker_name}(...).{send_name}",
            "to": to,
            "message_len": len(body.message),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream TextNow error: {e}")
