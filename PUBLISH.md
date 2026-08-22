# 🚀 Publishing DealSpot

DealSpot is a **Progressive Web App (PWA)** with optional **real push notifications**.
It installs to a phone's home screen like a native app — no app store required.

## 1. Deploy to Render (free)
1. Push this folder to a public/private GitHub repo.
2. On https://render.com → **New + → Blueprint** → pick the repo (`render.yaml` configures it).
   Or create a **Web Service** manually:
   - Runtime: **Python 3**
   - Build command: `pip install -r requirements.txt`
   - Start command: `python server.py`
   - Plan: **Free**
3. After deploy you get `https://YOUR-NAME.onrender.com`.

## 2. Enable real push notifications (one-time)
In the Render dashboard → your service → **Environment**, add these variables:

| Key | Value |
|-----|-------|
| `VAPID_PUBLIC_KEY`  | `BDWLfn-UImc8KC2Owztzlblex9iZGiDNZh6V_q44nnwpnExfsV6fvaaffUXaBkJITGpK5SplSAbwWJkYPpvEfgQ` |
| `VAPID_PRIVATE_KEY` | `MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgyOjSH1awFVxqIARqb8iJzpfhPXP9cZP2hve9P4t8PJuhRANCAAQ1i35_lCJnPCgtjsM7c5W5XsfYmRogzWYelf6uOJ58KZxMX7Fen72mn31F2gZCSExqSuUqZUgG8FiZGD6bxH4E` |
| `VAPID_SUBJECT`     | `mailto:you@yourdomain.com` (any contact email) |
| `PUSH_PUBLIC_BASE_URL` | `https://YOUR-NAME.onrender.com` (no trailing slash) |

Then **Manual Deploy → Clear build cache & deploy**. The keys above were generated
for your copy of the app; they're safe to use but for a serious launch you can
regenerate your own pair (see below).

### Regenerate your own VAPID keys (optional)
```python
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import base64
k = ec.generate_private_key(ec.SECP256R1())
pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
priv = k.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
b = lambda x: base64.urlsafe_b64encode(x).rstrip(b'=').decode()
print("VAPID_PUBLIC_KEY="+b(pub)); print("VAPID_PRIVATE_KEY="+b(priv))
```

## 3. Keep the server awake (important for push!)
Render's free plan sleeps after ~15 min idle, which delays push notifications.
Keep it awake with a free cron that pings the health endpoint every 10 minutes:
- **cron-job.org** (free): create a job hitting `https://YOUR-NAME.onrender.com/healthz` every 10 min.
- Or **UptimeRobot** (free), same URL, 5-min interval.

Even without this, the app still works — the first visit after idle just takes ~20s.

## 4. Install on a phone
- **Android (Chrome):** open the URL → ⋮ → **Install app**. Then enable
  **Phone push notifications** in Settings; the system prompt will appear.
- **iPhone (Safari, iOS 16.4+):** Share ⬆️ → **Add to Home Screen**. Open the
  installed icon, then enable push in Settings (iOS only allows push for
  apps added to the home screen).

Push fires when the server sees a *new* deal matching your saved keywords/stores/
minimum discount while the app is closed.

## 5. Notes
- Real push works on Android Chrome/Edge and on iOS 16.4+ **for installed PWAs**.
- Your data (watchlist, settings, keywords) stays on the device. The server only
  stores an anonymous push token + your alert preferences.
- Live deal data comes from Slickdeals' public RSS. For a monetized launch,
  switch to official affiliate APIs (Amazon PA-API, Walmart, Best Buy, Target).
