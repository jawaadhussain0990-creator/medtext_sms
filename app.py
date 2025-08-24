import os
import time
import inspect
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

# Unofficial TextNow wrapper
from pythontextnow import Client

# Cache the client ~30 minutes
_CLIENT = None
_CLIENT_TS = 0


def get_client():
    """
    Works with multiple pythontextnow variants:
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
        # Fallback: no-arg then login method
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


app = FastAPI(title="TextNow SMS Microservice (Unofficial)", version="1.1.0")

# CORS: open for your MVP; tighten allow_origins later if you want
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


def find_send_callable(client):
    """
    Dynamically discover a method on the client that can send an SMS.
    We prefer obvious names first, then scan fallbacks.
    Returns (callable, param_names_list)
    """
    preferred = [
        "send_sms", "send_message", "send_text", "text", "send", "sms", "message",
    ]
    for name in preferred:
        if hasattr(client, name) and callable(getattr(client, name)):
            fn = getattr(client, name)
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                sig = None
            params = list(sig.parameters.keys()) if sig else []
            return fn, params

    # Fallback: scan everything for 'send'/'text' in the name
    for name, fn in inspect.getmembers(client, predicate=callable):
        lname = name.lower()
        if any(k in lname for k in ("send", "text", "sms", "message")) and not lname.startswith("_"):
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                sig = None
            params = list(sig.parameters.keys()) if sig else []
            return fn, params

    return None, []


def try_invoke_send(fn, params, to, message):
    """
    Try common positional and keyword combinations to call the discovered send function.
    """
    # Common phone param names and message param names we might see
    phone_keys = ["to", "phone", "number", "recipient", "contact", "send_to"]
    msg_keys = ["message", "text", "body", "content"]

    # 1) Positional (to, message)
    try:
        return fn(to, message)
    except TypeError:
        pass
    # 2) Positional swapped (message, to)
    try:
        return fn(message, to)
    except TypeError:
        pass
    # 3) Keyword (to=, message=)
    for pkey in phone_keys:
        for mkey in msg_keys:
            try:
                return fn(**{pkey: to, mkey: message})
            except TypeError:
                continue
    # 4) Keyword with only message (some libs infer last chat)
    for mkey in msg_keys:
        try:
            return fn(**{mkey: message})
        except TypeError:
            continue

    raise RuntimeError("Could not call the discovered send method with known argument patterns.")


@app.get("/health")
def health():
    return {"ok": True, "service": "textnow", "version": "1.1.0"}


@app.get("/introspect")
def introspect():
    """
    Helps us see what's on the Client when debugging.
    Returns the methods that look like they could send messages and their parameter names.
    """
    try:
        client = get_client()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Client init failed: {e}")

    candidates = []
    for name, fn in inspect.getmembers(client, predicate=callable):
        lname = name.lower()
        if any(k in lname for k in ("send", "text", "sms", "message")) and not lname.startswith("_"):
            try:
                sig = inspect.signature(fn)
                params = list(sig.parameters.keys())
            except Exception:
                sig = None
                params = []
            candidates.append({"name": name, "params": params})
    return {"candidates": candidates}


@app.post("/send")
def send_sms(body: SendBody):
    to = normalize_number(body.to)
    try:
        client = get_client()
        fn, params = find_send_callable(client)
        if not fn:
            raise RuntimeError("Could not find a send method on pythontextnow.Client")
        try_invoke_send(fn, params, to, body.message)
        return {"ok": True, "to": to, "message_len": len(body.message)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream TextNow error: {e}")
