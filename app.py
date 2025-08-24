import os
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

# unofficial TextNow wrapper
from pythontextnow import Client  # works with different variants of the lib

# cache the client for ~30 minutes
_CLIENT = None
_CLIENT_TS = 0


def get_client():
    """
    Supports BOTH pythontextnow variants:
    - Variant A: Client(email, password)  OR  Client("", sid_cookie=...)
    - Variant B: Client() then login via a method:
        * client.login(email, password)  or client.log_in(...) / client.sign_in(...)
        * client.login_sid(cookie)       or client.log_in_sid(...) / client.login_with_sid(...) / client.set_sid(...)
    We try constructor-first, fall back to no-arg + login method.
    """
    global _CLIENT, _CLIENT_TS

    email = os.environ.get("TEXTNOW_EMAIL")
    password = os.environ.get("TEXTNOW_PASSWORD")
    sid_cookie = os.environ.get("TEXTNOW_SID_COOKIE")  # optional alternative auth

    if not ((email and password) or sid_cookie):
        raise RuntimeError("TEXTNOW_EMAIL (and TEXTNOW_PASSWORD) or TEXTNOW_SID_COOKIE must be set.")

    # reuse client if fresh (< 30 minutes)
    if _CLIENT and (time.time() - _CLIENT_TS < 1800):
        return _CLIENT

    # ----- Try Variant A: args in constructor
    try:
        if sid_cookie:
            client = Client("", sid_cookie=sid_cookie)
        else:
            client = Client(email, password)
        _CLIENT = client
        _CLIENT_TS = time.time()
        return _CLIENT
    except TypeError:
        # If constructor doesn't accept args, fall back to Variant B
        client = Client()

        # cookie-based login first if provided
        if sid_cookie:
            for method_name in ("login_sid", "log_in_sid", "login_with_sid", "set_sid"):
                if hasattr(client, method_name):
                    getattr(client, method_name)(sid_cookie)
                    break
            else:
                # last resort: try setting an attribute directly
                try:
                    setattr(client, "sid_cookie", sid_cookie)
                except Exception:
                    pass
        else:
            # email/password login
            for method_name in ("login", "log_in", "sign_in"):
                if hasattr(client, method_name):
                    getattr(client, method_name)(email, password)
                    break
            else:
                raise RuntimeError("Could not find a login(email, password) method on pythontextnow.Client")

        _CLIENT = client
        _CLIENT_TS = time.time()
        return _CLIENT


app = FastAPI(title="TextNow SMS Microservice (Unofficial)", version="1.0.0")

# open CORS so you can call from medtext.online directly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # tighten later if you want
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SendBody(BaseModel):
    to: str = Field(..., description="10-digit US or E.164 like +18322966196")
    message: str = Field(..., min_length=1, max_length=480)


@app.get("/health")
def health():
    """Simple health check"""
    return {"ok": True, "service": "textnow", "version": "1.0.0"}


@app.post("/send")
def send_sms(body: SendBody):
    # normalize US 10-digit -> +1
    raw = body.to.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        to = "+1" + digits
    elif raw.startswith("+") and len(digits) >= 8:
        to = raw
    else:
        raise HTTPException(status_code=400, detail="Invalid phone number format. Use 10-digit US or E.164.")

    try:
        client = get_client()
        # different libs use different method names, but send_sms is common
        if hasattr(client, "send_sms"):
            client.send_sms(to, body.message)
        else:
            # fallbacks if the wrapper used a different name
            sent = False
            for m in ("send_message", "send", "sms", "text"):
                if hasattr(client, m):
                    getattr(client, m)(to, body.message)
                    sent = True
                    break
            if not sent:
                raise RuntimeError("Could not find a send method on pythontextnow.Client")

        return {"ok": True, "to": to, "message_len": len(body.message)}
    except HTTPException:
        raise
    except Exception as e:
        # bubble up useful info but keep 502 so your frontend sees a clean error
        raise HTTPException(status_code=502, detail=f"Upstream TextNow error: {e}")
