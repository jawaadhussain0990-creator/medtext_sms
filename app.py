import os
import time
import inspect
from collections import deque
from typing import Callable, List, Tuple, Set, Any, Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

# Unofficial TextNow wrapper (your uploaded wheel)
from pythontextnow import Client

# Cache
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


app = FastAPI(title="TextNow SMS Microservice (Unofficial)", version="1.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if desired
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


# ----------------- deep discovery helpers -----------------

SEND_HINTS = ("send", "text", "sms", "message")


def _method_sig(fn: Callable):
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None


def _safe_getattr(obj: Any, name: str):
    try:
        return getattr(obj, name)
    except Exception:
        return object()  # sentinel


def _iter_callables_deep(root: Any, max_depth: int = 3, max_items: int = 2000) -> List[Tuple[str, Callable, List[str]]]:
    """
    Breadth-first traversal up to max_depth over attributes.
    Returns list of (path, fn, params)
    """
    results: List[Tuple[str, Callable, List[str]]] = []
    visited_ids: Set[int] = set()
    q: deque[Tuple[str, Any, int]] = deque()
    q.append(("client", root, 0))

    def add_callable(path: str, fn: Callable):
        sig = _method_sig(fn)
        params = list(sig.parameters.keys()) if sig else []
        results.append((path, fn, params))

    processed = 0
    while q and processed < max_items:
        path, obj, depth = q.popleft()
        processed += 1

        if id(obj) in visited_ids:
            continue
        visited_ids.add(id(obj))

        # collect methods on obj
        for name, fn in inspect.getmembers(obj, predicate=callable):
            if name.startswith("_"):
                continue
            add_callable(f"{path}.{name}", fn)

        # queue sub-attributes if depth allows
        if depth >= max_depth:
            continue

        # enumerate attributes
        for name in dir(obj):
            if name.startswith("_"):
                continue
            sub = _safe_getattr(obj, name)
            if sub is object():  # failed getattr
                continue

            # Avoid recursing into primitives, strings, modules, classes
            if isinstance(sub, (str, bytes, bytearray, int, float, bool)):
                continue
            if inspect.ismodule(sub) or inspect.isclass(sub) or inspect.ismethoddescriptor(sub):
                continue

            q.append((f"{path}.{name}", sub, depth + 1))

    return results


def _score_path(path: str) -> int:
    p = path.lower()
    s = 0
    if p.count(".") == 1:
        s += 1
    for h in SEND_HINTS:
        if h in p:
            s += 2
    if any(k in p for k in ("send_sms", "send_message", "messages.send", "text")):
        s += 3
    return s


def find_send_candidates(client) -> List[Tuple[str, Callable, List[str]]]:
    methods = _iter_callables_deep(client, max_depth=3)
    # Filter to those that look like senders
    cands = [(path, fn, params) for (path, fn, params) in methods if any(h in path.lower() for h in SEND_HINTS)]
    # Rank by heuristic
    cands.sort(key=lambda t: _score_path(t[0]), reverse=True)
    # Deduplicate by path
    dedup: Dict[str, Tuple[str, Callable, List[str]]] = {}
    for path, fn, params in cands:
        dedup[path] = (path, fn, params)
    return list(dedup.values())


def try_invoke_send(fn: Callable, params: List[str], to: str, message: str):
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
    # 3) keyword permutations
    for pkey in phone_keys:
        for mkey in msg_keys:
            try:
                return fn(**{pkey: to, mkey: message})
            except TypeError:
                continue
    # 4) message-only
    for mkey in msg_keys:
        try:
            return fn(**{mkey: message})
        except TypeError:
            continue

    raise RuntimeError("Could not call the discovered send method with known argument patterns.")


# ----------------- endpoints -----------------

@app.get("/health")
def health():
    return {"ok": True, "service": "textnow", "version": "1.3.0"}


@app.get("/debug")
def debug():
    """
    Returns a snapshot of attributes and all candidate method paths up to depth=3.
    """
    try:
        client = get_client()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Client init failed: {e}")

    # quick attribute preview (first 100 names)
    attrs = [n for n in dir(client) if not n.startswith("_")][:100]

    # candidate methods
    cands = []
    for path, fn, params in find_send_candidates(client):
        cands.append({"path": path, "params": params})

    return {"attrs_preview": attrs, "candidates": cands}


@app.post("/send")
def send_sms(body: SendBody):
    to = normalize_number(body.to)
    try:
        client = get_client()
        candidates = find_send_candidates(client)
        if not candidates:
            raise RuntimeError("No candidate send methods found on pythontextnow.Client (searched up to depth=3).")

        last_err = None
        for path, fn, params in candidates:
            try:
                try_invoke_send(fn, params, to, body.message)
                return {"ok": True, "used": path, "to": to, "message_len": len(body.message)}
            except Exception as e:
                last_err = e
                continue

        if last_err:
            raise RuntimeError(f"Found {len(candidates)} candidates; last error: {last_err}")
        raise RuntimeError("Discovered candidates but invocation failed without details.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream TextNow error: {e}")
