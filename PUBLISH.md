# 🚀 Publishing DealSpot — the easy way

DealSpot is now a **Progressive Web App (PWA)**. That means you can "install" it
to any phone's home screen — no Google Play / App Store, no fees, no review wait.

## 1. Put it online (free, ~5 minutes)
The app needs to be hosted at an `https://` address for the install prompt and
offline mode to work. Easiest free options:

### Option A — Render (recommended, runs the Python backend)
1. Push the `deals-app` folder to a **GitHub** repository.
2. Go to https://render.com → **New** → **Web Service** → connect the repo.
3. Settings:
   - **Runtime:** Python 3
   - **Build command:** `pip install --upgrade pip` (no extra deps needed — only stdlib)
   - **Start command:** `python server.py`
   - **Environment variable:** `PORT` = `10000` (Render sets this automatically)
4. Click **Deploy**. You get a free `https://your-app.onrender.com` URL.

### Option B — any static host (Netlify / Vercel / GitHub Pages)
The live `/api/deals` backend won't run on a static host. In that case the app
automatically falls back to its bundled sample deals. Everything else
(install, dark mode, alerts, watchlist) still works.

## 2. Install on a phone
- **Android (Chrome):** open the URL → tap the **⋮** menu → **Install app** /
  **Add to Home screen**. A Play-style install prompt also appears automatically.
- **iPhone / iPad (Safari):** open the URL → tap the **Share ⬆️** button →
  **Add to Home Screen**. (Safari requires this; the app shows these instructions.)

It launches full-screen with its own icon, like a native app.

## 3. (Optional) Later, put it on the app stores
If you eventually want an official Play Store / App Store listing:
- Wrap the same files with **Capacitor** (`npx cap init` → add android/ios → build).
- Google Play: $25 one-time developer fee.
- Apple App Store: $99/year + a Mac to build.
- Before submitting publicly, replace the Slickdeals RSS feed with official
  affiliate APIs (Amazon PA-API, Walmart, Best Buy, Target) and add a privacy
  policy — that keeps you within each platform's and retailer's terms.

## What was added for PWA support
- `manifest.webmanifest` — app name, icons, theme colors, shortcuts
- `sw.js` — service worker (offline shell + cached API responses)
- `icons/` — 192/512 + maskable + Apple touch icons
- Install button + iOS instructions under **Settings → About**
