import os
import time
import inspect
from typing import Callable, List, Tuple
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

# Unofficial TextNow wrapper (your uploaded wheel)
from pythontextnow import Client

# Cache the client for 30 minutes
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


app = FastAPI(title="TextNow SMS Microservice (Unofficial)", version="1.2.0")

# Open CORS for your MVP; tighten later if you want
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


# ---------- dynamic discovery helpers ----------

SEND_NAME_HINTS = ("send", "text", "sms", "message")


def _method_signature(fn: Callable):
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None


def _collect_methods(obj, prefix: str = "") -> List[Tuple[str, Callable]]:
    """
    Collect callables on obj (depth 0) and on its immediate attributes (depth 1).
    Returns list of (path, fn), where path looks like 'client.send_sms' or 'client.messages.send'
    """
    results: List[Tuple[str, Callable]] = []

    # Depth 0: methods directly on obj
    for name, fn in inspect.getmembers(obj, predicate=callable):
        if name.startswith("_"):
            continue
        results.append((f"{prefix}{name}", fn))

    # Depth 1: look into public attributes; if attribute has callables, collect them
    for attr_name in dir(obj):
        if attr_name.startswith("_"):
            continue
        try:
            attr = getattr(obj, attr_name)
        except Exception:
            continue
        # only drill into simple objects (avoid drilling into modules/classes)
        if callable(attr):
            # already captured above
            continue
        # scan callables on the attribute
        for name, fn in inspect.getmembers(attr, predicate=callable):
            if name.startswith("_"):
                continue
            results.append((f"{prefix}{attr_name}.{name}", fn))

    return results


def find_send_candidates(client) -> List[Tuple[str, Callable, List[str]]]:
    """
    Return list of candidate send functions as (path, fn, param_names).
    We prefer obvious names first, but include everything that looks relevant.
    """
    methods = _collect_methods(client, prefix="client.")
    candidates: List[Tuple[str, Callable, List[str]]] = []

    # score preferred names higher
    def score(path: str) -> int:
        p = path.lower()
        s = 0
        # direct methods get a tiny boost
        if p.count(".") == 1:
            s += 1
        for hint in SEND_NAME_HINTS:
            if hint in p:
                s += 2
        # heavily weight common combos
        if any(k in p for k in ("send_sms", "send_message", "text", "messages.send")):
            s += 3
        return s

    ranked = sorted(methods, key=lambda x: score(x[0]), reverse=True)

    for path, fn in ranked:
        lp = path.lower()
        if any(h in lp for h in SEND_NAME_HINTS):
            sig = _method_signature(fn)
            params = list(sig.parameters.keys()) if sig else []
            candidates.append((path, fn, params))

    return candidates


def try_invoke_send(fn: Callable, params: List[str], to: str, message: str):
    """
    Try several positional/keyword combos to call the method.
    """
    phone_keys = ["to", "phone", "number", "recipient", "contact", "send_to"]
    msg_keys = ["message", "text", "body", "content"]

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
    # 3) keyword combos
    for pkey in phone_keys:
        for mkey in msg_keys:
            try:
                return fn(**{pkey: to, mkey: message})
            except TypeError:
                continue
    # 4) message-only (some libs send to last/active chat)
    for mkey in msg_keys:
        try:
            return fn(**{mkey: message})
        except TypeError:
            continue

    raise RuntimeError("Could not call the discovered send method with known argument patterns.")


# --------------- endpoints ---------------

@app.get("/health")
def health():
    return {"ok": True, "service": "textnow", "version": "1.2.0"}


@app.get("/debug")
def debug():
    """
    Show all candidate methods we think might send messages, including sub-objects.
    """
    try:
        client = get_client()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Client init failed: {e}")

    cands = []
    for path, fn, params in find_send_candidates(client):
        cands.append({"path": path, "params": params})
    return {"candidates": cands}


@app.post("/send")
def send_sms(body: SendBody):
    to = normalize_number(body.to)
    try:
        client = get_client()
        candidates = find_send_candidates(client)
        if not candidates:
            raise RuntimeError("No candidate send methods found on pythontextnow.Client (or its sub-objects).")

        # Try candidates in order until one succeeds
        last_err = None
        for path, fn, params in candidates:
            try:
                try_invoke_send(fn, params, to, body.message)
                return {"ok": True, "used": path, "to": to, "message_len": len(body.message)}
            except Exception as e:
                last_err = e
                continue

        # If none succeeded:
        if last_err:
            raise RuntimeError(f"Discovered {len(candidates)} candidates but all failed. Last error: {last_err}")
        raise RuntimeError("Discovered candidates but invocation failed without error details.")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream TextNow error: {e}")
