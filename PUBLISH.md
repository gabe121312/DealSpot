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

## 4. Turn on Premium payments (Stripe) — make money 💰

DealSpot now has a built-in **Premium** upgrade: members see every new deal
**8 minutes before** free users, get **instant** push alerts (free users wait
8 min), and **unlimited keywords** (free = 2). Everything is handled by the
app — you just connect Stripe.

### One-time setup (~10 minutes)

1. **Create a free Stripe account** at https://dashboard.stripe.com/register
   (use "Save changes" / activate later — you can test everything first;
   Stripe asks for bank details before *real* money can be paid out, not to sign up).

2. **Create your product.** In the Stripe dashboard:
   **Product catalog → Add product** → name it `DealSpot Premium` →
   **Recurring** → price `2.99 USD` per month.
   *(Prefer one-time? Choose **One time**, e.g. `$14.99` — everything else is identical.)*

3. **Create the payment link.** **Payment links → New link** → pick
   `DealSpot Premium` → scroll to **After payment → Redirect customers to your website**:
   - URL: `https://YOUR-NAME.onrender.com/?session_id={CHECKOUT_SESSION_ID}`
   ⚠️ The `{CHECKOUT_SESSION_ID}` part must be typed exactly like that —
   it's how the app knows the payment really happened. Copy the link when created.

4. **Get your secret key.** **Developers → API keys → Reveal** the
   **Secret key** (looks like `sk_live_...` or `sk_test_...`).

5. **Add these in Render → Environment:**

| Key | Value |
|-----|-------|
| `STRIPE_SECRET_KEY` | your `sk_live_...` (or `sk_test_...` while testing) key |
| `STRIPE_PAYMENT_URL` | the payment link URL from step 3 (`https://buy.stripe.com/...`) |
| `PREMIUM_PRICE_LABEL` | what to show, e.g. `$2.99/month` |
| `PREMIUM_SECRET`   | `db2da09d9f7c5d1fc1ccd32d67e547a7492f32b9cd60ebe88e46751f378002c3` (never change after launch!) |
| `PREMIUM_EARLY_MINUTES` | `8` (free-user delay — you already set this in render.yaml) |

### Add an ANNUAL plan (optional, recommended 💰)
Two prices = more choice = more upgrades ("Annual — best value" appears under
the monthly button automatically):
1. Stripe → **Product catalog** → your `DealSpot Premium` product → **Add price**
   → **Recurring, yearly**, e.g. `$19.99` per year → Save.
2. **Payment links → + New** → pick the *yearly* price → same **After payment**
   redirect URL: `https://YOUR-NAME.onrender.com/?session_id={CHECKOUT_SESSION_ID}`
   → copy the new link.
3. Add in Render → Environment:

| Key | Value |
|-----|-------|
| `STRIPE_PAYMENT_URL_YEARLY` | the annual payment link URL |
| `PREMIUM_PRICE_LABEL_YEARLY` | e.g. `$19.99/year` |

No code change needed — the second button appears on its own once set.

6. **Manual Deploy → Clear build cache & deploy.**

7. **Test it:** open your app → Settings → ⚡ Premium → **Go Premium** →
   use Stripe's test card `4242 4242 4242 4242` (any future date, any CVC)
   with a *test-mode* link → you should land back in DealSpot with a 👑.

### How the money works
- Stripe charges **2.9% + 30¢** per payment — no monthly fee.
- Payouts go to your bank account from the Stripe dashboard (Payouts tab).
- 100 members at $2.99/month ≈ **$290/month** after Stripe fees.
- Monthly memberships auto-renew; members can cancel from
  Settings → Premium → Manage subscription.
- Refunds/cancellations: Stripe dashboard → Payments → refund.

### Changing the free delay
`PREMIUM_EARLY_MINUTES` controls how long free users wait (default 8).
Set it to `15` or `5` — the app updates the wording automatically.

## 4b. Paddle payments (the no-tax-headache alternative) 💳

DealSpot supports **Paddle** as a drop-in replacement for Stripe. Paddle is a
*merchant of record* — they're the legal seller, so they handle sales tax,
invoices, and identity paperwork. Great if Stripe's identity verification is
being difficult (ITIN holders, we see you).

**Setup (test first in sandbox):**
1. Create a free account at https://www.paddle.com (start with a **sandbox**
   account for testing — no review needed).
2. **Catalog → Products → New product**: `DealSpot Premium`, add two prices:
   `$2.99 USD / month recurring` and `$19.99 USD / year recurring`.
   Copy both **price IDs** (they look like `pri_01h…`).
3. **Developer tools → Authentication**: generate an **API key** (secret 🔒)
   and a **client-side token** (public — safe in the app).
4. Add these in Render → Environment:

| Key | Value |
|-----|-------|
| `PADDLE_API_KEY` | secret API key 🔒 |
| `PADDLE_CLIENT_TOKEN` | public client-side token |
| `PADDLE_PRICE_MONTHLY` | `pri_…` of the monthly price |
| `PADDLE_PRICE_YEARLY` | `pri_…` of the annual price |
| `PADDLE_ENV` | `sandbox` for testing, `live` for real money |

5. Deploy. The ⚡ Premium buttons now open DealSpot's own checkout page
   (`/checkout`) with Paddle's secure overlay. Monthly + annual work the same
   as with Stripe. Test card in sandbox: `4242 4242 4242 4242`.

**Notes:**
- If both Stripe and Paddle env vars are set, Stripe wins. To use Paddle,
  remove/empty the Stripe keys (`STRIPE_SECRET_KEY`, `STRIPE_PAYMENT_URL`).
- Going live: Paddle reviews live accounts (they check your website — a few
  days). Switch `PADDLE_ENV` to `live` and use live keys once approved.
- Paddle handles sales tax everywhere — no tax checklist, no filings. Their
  fee is ~5% + 50¢ per transaction (a bit more than Stripe, that's the
  trade-off for zero paperwork).

## 5. Extras (all optional, all free) — set any time in Render → Environment

| Key | What it does |
|-----|--------------|
| `AMAZON_TAG` | Your Amazon Associates tag (e.g. `yourname-20`). The moment you set it, **every Amazon link in the app automatically earns you commission** when people shop. Sign up free: https://affiliate-program.amazon.com |
| `EBAY_CAMPID` | Same idea for eBay (eBay Partner Network id) |
| `TIP_URL` | A "☕ Tip jar" button appears in Settings → About. Point it at your Ko-fi page (free at https://ko-fi.com) |
| `RESEND_API_KEY` | Turns ON the weekly Top-10 email digest. Sign up free at https://resend.com (just email + password, no tax ID), create an API key, paste it here. Sends every 7 days to everyone subscribed in the app |
| `REFUND_EMAIL` | Optional: your email — adds an "Email DealSpot support" button to Settings → Premium → Billing & refunds (refunds themselves are one click in Paddle → Transactions) |
| `MYMEMORY_EMAIL` | Optional but recommended: your email, raises the free translation quota for automatic deal-title translation (24 languages). Translations are cached — each title is only ever translated once per language |
| `DIGEST_FROM` | Optional custom sender for digest emails, e.g. `DealSpot <hello@yourdomain.com>` (on Resend's free tier you can also leave the default) |

No redeploy needed — saving the env var restarts the service and the feature activates.

**Also new in the app itself (no setup needed):**
- 🌍 **24 languages** — Settings → Appearance → Language (or 🌐 Auto, which follows the phone's language). Español, Français, Deutsch, Português, Italiano, Nederlands, Polski, Türkçe, Русский, Українська, العربية, עברית, हिन्दी, বাংলা, اردو, 中文（简/繁）, 日本語, 한국어, Tiếng Việt, ไทย, Bahasa Indonesia, Filipino, Ελληνικά. Arabic/Hebrew/Urdu flip the whole app right-to-left. Translations live in **lang.js — keep that file with the app** (if it's ever missing, DealSpot quietly falls back to English). Improve any translation by editing lang.js; each language is one list.
- 🔥💀 **Community voting** — users mark deals "still good" or "dead"; deals with 3+ dead votes sink to the bottom. Votes are stored on the server (votes.json — note: on Render's free tier, stored votes reset when the service restarts; upgrade to a paid disk later if the community grows).
- 📣 **Share DealSpot** button in Settings → About.
- 📧 **Weekly digest signup box** in Settings (collects emails immediately; sending starts the moment `RESEND_API_KEY` is set).

## 6. Install on a phone
- **Android (Chrome):** open the URL → ⋮ → **Install app**. Then enable
  **Phone push notifications** in Settings; the system prompt will appear.
- **iPhone (Safari, iOS 16.4+):** Share ⬆️ → **Add to Home Screen**. Open the
  installed icon, then enable push in Settings (iOS only allows push for
  apps added to the home screen).

Push fires when the server sees a *new* deal matching your saved keywords/stores/
minimum discount while the app is closed. Premium members get it **instantly**;
free users get it when the deal unlocks for everyone (8 min later).

## 7. Notes
- Real push works on Android Chrome/Edge and on iOS 16.4+ **for installed PWAs**.
- Your data (watchlist, settings, keywords) stays on the device. The server only
  stores an anonymous push token + your alert preferences.
- Live deal data comes from Slickdeals' public RSS. For a monetized launch,
  switch to official affiliate APIs (Amazon PA-API, Walmart, Best Buy, Target).
