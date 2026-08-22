# 🏷️ DealSpot

A mobile-style **live discount tracker** that aggregates deals from Amazon, Walmart,
Sam's Club, Best Buy, Target, and more — with keyword price alerts, watchlist,
dark mode, location-based in-store deals, and a tap-to-buy link on every product.

It's a **Progressive Web App (PWA)**, so it installs to a phone's home screen
like a native app — no app store required.

## ✨ Features
- **Live deal feed** pulled from Slickdeals' public RSS (cached 5 min).
- **Store tabs** — Amazon, Walmart, Sam's Club, Best Buy, Target, Other, Watchlist, Near You.
- **Keyword & store alerts** with an in-app notification center and browser push.
- **Location / ZIP** — resolves your city & state and surfaces in-store/YMMV deals.
- **Dark / light / system theme**, fully theme-aware.
- **Tap any deal** to open the real retailer product page; share button on every card.
- **Pull-to-refresh**, skeleton loaders, PWA install, offline shell (service worker).
- **Privacy-first** — all data stays in your browser; a built-in privacy policy.

## 🚀 Run locally
```bash
python server.py
# open http://localhost:8080
```
Only the Python standard library is needed (Python 3.8+).

## 🌐 Deploy free
**Easiest:** push to GitHub, then on [Render](https://render.com) create a
**Blueprint** — `render.yaml` configures everything automatically. Or create a
Web Service manually:
- Runtime: Python 3
- Build: `pip install --upgrade pip`
- Start: `python server.py`

You'll get an `https://...onrender.com` URL. Open it on a phone and use
**Add to Home Screen** (Android) or **Share → Add to Home Screen** (iOS).

## 📁 Files
| File | Purpose |
|------|---------|
| `server.py` | Serves the app + `/api/deals` live feed |
| `index.html` | The full mobile app (UI + logic) |
| `manifest.webmanifest` | PWA install metadata |
| `sw.js` | Service worker (offline + caching) |
| `icons/` | App icons |
| `render.yaml`, `Procfile`, `requirements.txt` | One-click deploy config |
| `PUBLISH.md` | Step-by-step publishing walkthrough |

## ⚠️ Note on data
Live data comes from Slickdeals' public RSS feed (great for personal use). For a
large/monetized public launch, switch to official affiliate/retailer APIs
(Amazon PA-API, Walmart Affiliate, Best Buy API, Target) and keep the affiliate
disclosure in the app's Privacy page up to date.
