
# TextNow SMS Microservice (Unofficial)

This is a tiny FastAPI service that exposes **/send** for sending SMS through a TextNow account
using the **pythontextnow** package (unofficial). Use at your own risk—this may violate TextNow
Terms of Service and can break without notice.

## Endpoints
- `GET /health` → basic health check
- `POST /send` → JSON: `{ "to": "+18322966196", "message": "Hello" }`

## Local run

1) Create and populate a `.env` (see `example.env`).
2) Install deps:

   ```bash
   pip install -r requirements.txt
   ```

3) Run:

   ```bash
   uvicorn app:app --reload --port 8080
   ```

## Deploy to Render (free tier)
1) Create a new **Web Service** and connect this folder as a repo (or upload zip).
2) Set **Build Command**: `pip install -r requirements.txt`
3) Set **Start Command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`
4) Add **Environment Variables**: `TEXTNOW_EMAIL`, `TEXTNOW_PASSWORD` (or `TEXTNOW_SID_COOKIE`).
5) Deploy → you’ll get a public URL like `https://your-service.onrender.com`.

## Using from your website (Lovable)

Front-end example:
```html
<form id="smsForm">
  <input name="phone" placeholder="10-digit phone" required />
  <textarea name="message" maxlength="160" placeholder="Your text…" required></textarea>
  <button>Send</button>
</form>
<script>
const API_URL = 'https://YOUR-RENDER-URL/send';
document.getElementById('smsForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const phone = e.target.phone.value.replace(/\D/g,'');
  const message = e.target.message.value.trim();
  const r = await fetch(API_URL, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ to: phone, message })
  });
  const data = await r.json();
  alert(r.ok ? 'Sent!' : ('Failed: ' + (data.detail || 'unknown')));
});
</script>
```

Or via Make.com:
- Trigger: Webhooks → Custom webhook (your site posts `{to,message}` here).
- Action: HTTP → Make a request
  - Method: POST
  - URL: `https://YOUR-RENDER-URL/send`
  - Headers: `Content-Type: application/json`
  - Body: `{ "to": "{{1.to}}", "message": "{{1.message}}" }`

## Notes
- Keep messages short (<160 chars) to avoid splitting.
- This relies on TextNow’s private endpoints; breakage is possible.
