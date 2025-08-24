
import os
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from pythontextnow import Client  # relies on included wheel
from fastapi.middleware.cors import CORSMiddleware

# Simple cache for the TextNow client
_CLIENT = None
_CLIENT_TS = 0

def get_client():
    global _CLIENT, _CLIENT_TS
    email = os.environ.get("TEXTNOW_EMAIL")
    password = os.environ.get("TEXTNOW_PASSWORD")
    sid_cookie = os.environ.get("TEXTNOW_SID_COOKIE")  # optional alternative auth

    if not email and not sid_cookie:
        raise RuntimeError("TEXTNOW_EMAIL (and TEXTNOW_PASSWORD) or TEXTNOW_SID_COOKIE must be set.")

    # refresh client every 30 minutes to avoid stale sessions
    if _CLIENT and (time.time() - _CLIENT_TS < 1800):
        return _CLIENT

    if sid_cookie:
        client = Client(email or "", sid_cookie=sid_cookie)
    else:
        if not password:
            raise RuntimeError("TEXTNOW_PASSWORD must be set when using TEXTNOW_EMAIL auth.")
        client = Client(email, password)

    _CLIENT = client
    _CLIENT_TS = time.time()
    return _CLIENT

app = FastAPI(title="TextNow SMS Microservice", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SendBody(BaseModel):
    to: str = Field(..., description="E.164 number like +18322966196 or 10-digit US")
    message: str = Field(..., min_length=1, max_length=480)

@app.get("/health")
def health():
    return {"ok": True, "service": "textnow", "version": "1.0.0"}

@app.post("/send")
def send_sms(body: SendBody):
    # Normalize US 10-digit to +1
    to = body.to.strip()
    digits = ''.join(filter(str.isdigit, to))
    if len(digits) == 10:
        to = "+1" + digits
    elif to.startswith("+"):
        to = to
    else:
        raise HTTPException(status_code=400, detail="Invalid phone number format. Use 10-digit US or E.164.")

    try:
        client = get_client()
        client.send_sms(to, body.message)
        return {"ok": True, "to": to, "message_len": len(body.message)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream TextNow error: {e}")
