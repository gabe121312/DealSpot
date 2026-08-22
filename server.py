#!/usr/bin/env python3
"""
DealSpot backend
----------------
- Serves the PWA (index.html, manifest, icons, service worker)
- GET  /api/deals       -> live aggregated deals from Slickdeals RSS (cached 5 min)
- POST /api/push/sub    -> register a device for push notifications
- POST /api/push/unsub  -> remove a device
- GET  /api/push/pubkey -> VAPID public key the browser needs to subscribe

Push notifications: when the feed is refreshed and a NEW deal matches a
subscriber's saved keywords/stores/discount, the server sends a Web Push
message to their device. Works on Android Chrome and (iOS 16.4+) installed PWAs.

Configuration (set these in the Render dashboard → Environment):
  VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY  -> generated below, base64url
  VAPID_SUBJECT                        -> "mailto:you@example.com"
  PUSH_PUBLIC_BASE_URL                 -> https://dealspot-cn44.onrender.com
If VAPID keys are missing the server generates an ephemeral pair on startup
(push still works for that session but subscriptions won't survive restarts —
set real keys in production).
"""

from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs
import xml.etree.ElementTree as ET
import json, re, time, os, threading, base64, hashlib
from html import unescape

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    from pywebpush import webpush, WebPushException
    HAS_PUSH = True
except Exception:
    HAS_PUSH = False

PORT = int(os.environ.get("PORT", 8080))
CACHE_TTL = 300
HERE = os.path.dirname(os.path.abspath(__file__))

FEEDS = [
    "https://feeds.feedburner.com/SlickdealsnetFP",
    "https://slickdeals.net/forums/external.php?type=rss2&forumids=9",
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ── Retailer detection (Sam's Club must be checked before Walmart) ──────
RETAILERS = [
    ("amazon.", "amazon"), ("samsclub.com", "samsclub"),
    ("walmart.com", "walmart"), ("bestbuy.com", "bestbuy"),
    ("target.com", "target"), ("ebay.com", "ebay"),
    ("costco.com", "costco"), ("homedepot.com", "homedepot"),
    ("lowes.com", "lowes"), ("newegg.com", "newegg"),
    ("bjs.com", "bjs"), ("kohls.com", "kohls"),
    ("macys.com", "macys"), ("wayfair.com", "wayfair"),
]
TEXT_RETAILERS = [
    ("sams club", "samsclub"), ("samsclub", "samsclub"), ("sam's club", "samsclub"),
    ("walmart", "walmart"), ("amazon", "amazon"), ("best buy", "bestbuy"),
    ("bestbuy", "bestbuy"), ("target", "target"), ("ebay", "ebay"),
    ("costco", "costco"), ("home depot", "homedepot"), ("lowes", "lowes"),
    ("lowe's", "lowes"), ("newegg", "newegg"), ("bj's", "bjs"),
    ("bjs", "bjs"), ("kohl's", "kohls"), ("kohls", "kohls"),
    ("macy's", "macys"), ("macys", "macys"), ("wayfair", "wayfair"),
]
STORE_LABELS = {
    "amazon":"Amazon","walmart":"Walmart","bestbuy":"Best Buy","target":"Target",
    "ebay":"eBay","costco":"Costco","homedepot":"Home Depot","lowes":"Lowe's",
    "newegg":"Newegg","bjs":"BJ's","kohls":"Kohl's","macys":"Macy's",
    "wayfair":"Wayfair","samsclub":"Sam's Club","other":"Other Store",
}

US_STATES = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia",
    "HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa",
    "KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland",
    "MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi",
    "MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire",
    "NJ":"New Jersey","NM":"New Mexico","NY":"New York","NC":"North Carolina",
    "ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania",
    "RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota","TN":"Tennessee",
    "TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington",
    "WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming",
}
STATE_ABBR_SET = set(US_STATES.keys())
LOCAL_RE = re.compile(
    r"\b(?:in[- ]?store|in store only|bopus|buy online pick|pickup(?: in store)?|"
    r"curbside|clearance|ymmv|your mileage may vary|store only|in[- ]?person|"
    r"select stores?|select locations?|local deal)\b", re.I)
ONLINE_RE = re.compile(r"\b(free shipping|free s&?h|prime|online only|online deal|shipped to you)\b", re.I)

_cache = {"data": None, "ts": 0, "ids": set()}
_lock = threading.Lock()

# ── Push state ─────────────────────────────────────────────────────────
SUBS_FILE = os.path.join(HERE, "subscriptions.json")
_subs = []
_subs_lock = threading.Lock()
VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:dealspot@example.com")
PUBLIC_BASE_URL = os.environ.get("PUSH_PUBLIC_BASE_URL", "")


def _b64url_decode(s):
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _b64url_encode(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _ensure_vapid():
    """Load VAPID keys from env, or generate an ephemeral pair for dev."""
    global VAPID_PUBLIC, VAPID_PRIVATE
    if VAPID_PUBLIC and VAPID_PRIVATE:
        return
    key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    pub = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    priv = key.private_bytes(serialization.Encoding.DER,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    VAPID_PUBLIC = _b64url_encode(pub)
    VAPID_PRIVATE = _b64url_encode(priv)
    print("[push] No VAPID keys in env — generated EPHEMERAL keys. "
          "Set VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY for persistent push.")


def load_subscriptions():
    global _subs
    try:
        with open(SUBS_FILE) as f:
            _subs = json.load(f)
    except Exception:
        _subs = []


def save_subscriptions():
    try:
        tmp = SUBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_subs, f)
        os.replace(tmp, SUBS_FILE)
    except Exception as e:
        print("[push] could not save subs:", e)


def sub_matches(sub, deal):
    prefs = sub.get("prefs", {}) or {}
    kws = [k.lower() for k in prefs.get("keywords", []) if k]
    if kws:
        hay = (deal.get("name", "") + " " + deal.get("storeLabel", "")).lower()
        if not any(k in hay for k in kws):
            return False
    stores = prefs.get("stores", [])
    if stores and deal.get("store") not in stores:
        return False
    minp = int(prefs.get("minDiscount") or 0)
    if minp:
        sale, orig = deal.get("sale"), deal.get("orig")
        pct = round((1 - sale / orig) * 100) if (sale and orig and orig > sale) else 0
        if pct < minp:
            return False
    return True


def send_push(sub, payload):
    if not HAS_PUSH:
        return
    try:
        webpush(
            subscription_info=sub["subscription"],
            data=json.dumps(payload),
            vapid_private_key=_b64url_decode(VAPID_PRIVATE),
            vapid_claims={"sub": VAPID_SUBJECT},
        )
    except WebPushException as e:
        # 404/410 means the subscription is gone — remove it.
        if e.response and e.response.status_code in (404, 410):
            with _subs_lock:
                if sub in _subs:
                    _subs.remove(sub)
                    save_subscriptions()
        else:
            print("[push] send failed:", e)
    except Exception as e:
        print("[push] error:", e)


def broadcast_new_deals(new_deals):
    if not HAS_PUSH or not _subs or not new_deals:
        return
    # Limit how many we push per refresh so we don't spam devices.
    for sub in list(_subs):
        matches = [d for d in new_deals if sub_matches(sub, d)][:3]
        if not matches:
            continue
        if len(matches) == 1:
            d = matches[0]
            pct = 0
            if d.get("sale") and d.get("orig") and d["orig"] > d["sale"]:
                pct = round((1 - d["sale"] / d["orig"]) * 100)
            payload = {
                "title": f"🔥 {d.get('storeLabel','Deal')} deal" + (f" -{pct}%" if pct else ""),
                "body": d["name"][:140],
                "url": PUBLIC_BASE_URL + "/?deal=" + str(d["id"]) if PUBLIC_BASE_URL else (d.get("url") or "/"),
                "tag": "deal-" + str(d["id"]),
            }
        else:
            payload = {
                "title": f"🔥 {len(matches)} new matching deals",
                "body": " · ".join(m["name"][:50] for m in matches),
                "url": (PUBLIC_BASE_URL + "/") if PUBLIC_BASE_URL else "/",
                "tag": "deals-summary",
            }
        send_push(sub, payload)


# ── Deal parsing ───────────────────────────────────────────────────
def fetch_url(url, timeout=12):
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def detect_store(url, text=""):
    u = (url or "").lower()
    for n, k in RETAILERS:
        if n in u:
            return k
    t = (text or "").lower()
    for n, k in TEXT_RETAILERS:
        if n in t:
            return k
    return "other"


SKIP_HOSTS = ("slickdeals.net","feedburner","slickdealscdn.com","scorecardresearch",
              "gstatic","google-analytics","gravatar.com","reddit.com",
              "facebook.com","twitter.com","schema.org","w3.org")
SKIP_EXTS = (".jpg",".jpeg",".png",".gif",".webp",".svg",".css",".js",".ico")


def _is_product_url(u):
    low = u.lower()
    if not low.startswith("http"):
        return False
    if any(h in low for h in SKIP_HOSTS):
        return False
    if any(low.split("?")[0].endswith(e) for e in SKIP_EXTS):
        return False
    if "..." in low:
        return False
    return True


def extract_direct_link(desc, content):
    desc_clean = unescape(re.sub(r"<[^>]+>", " ", desc or ""))
    for m in re.findall(r'https?://[^\s<>"\')\]]+', desc_clean):
        u = m.rstrip(".,;!?:")
        if _is_product_url(u):
            return u
    blob = unescape(content or "")
    for m in re.findall(r'href=["\']([^"\']+)["\']', blob, re.I):
        if _is_product_url(m):
            return m
    for m in re.findall(r'https?://[^\s<>"\')\]]+', blob):
        u = m.rstrip(".,;!?:")
        if _is_product_url(u):
            return u
    return None


def extract_image(content, desc):
    for blob in (content, desc):
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', unescape(blob or ""), re.I)
        if m:
            src = m.group(1)
            if "slickdealscdn.com" in src or src.startswith("http"):
                return src
    return None


def extract_prices(text):
    amounts = [float(x) for x in re.findall(r'\$(\d+(?:\.\d{1,2})?)', text)]
    sale = amounts[0] if amounts else None
    orig = None
    m = re.search(r'(?:was|originally|reg(?:ular)?|msrp|list(?: price)?)\s*\$(\d+(?:\.\d{1,2})?)', text, re.I)
    if m:
        orig = float(m.group(1))
    elif len(amounts) > 1:
        orig = amounts[1]
    if orig and sale and orig <= sale:
        orig = None
    return sale, orig


def categorize(text):
    t = text.lower()
    tech_kw = ["tv","laptop","headphone","earbud","phone","tablet","ipad","macbook",
               "console","xbox","playstation","nintendo","switch","monitor","camera",
               "speaker","smartwatch","router","ssd","gpu","cpu","gaming","mouse",
               "keyboard","charger","vacuum robot"]
    home_kw = ["vacuum","cookware","mattress","towel","coffee","air fryer","instant pot",
               "grill","sofa","lamp","kitchen","blender","pressure cooker","stand mixer","knife"]
    if any(k in t for k in tech_kw): return "tech"
    if any(k in t for k in home_kw): return "home"
    return "other"


def detect_local(text):
    t = text or ""
    in_store = bool(LOCAL_RE.search(t))
    ymmv = "ymmv" in t.lower() or "your mileage" in t.lower()
    online_only = bool(ONLINE_RE.search(t)) and not in_store
    states = []
    for abbr, name in US_STATES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", t, re.I):
            states.append(abbr)
    if in_store or ymmv:
        for tok in re.findall(r"\b([A-Z]{2})\b", t):
            if tok in STATE_ABBR_SET and tok not in states:
                states.append(tok)
    return {"inStore": in_store or ymmv, "ymmv": ymmv,
            "onlineOnly": online_only, "states": states}


def parse_feed(xml_bytes):
    deals = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return deals
    ch = root.find("channel") or root
    CE = "{http://purl.org/rss/1.0/modules/content/}encoded"
    for it in ch.findall("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        content = ""
        ce = it.find(CE)
        if ce is not None and ce.text:
            content = ce.text
        direct = extract_direct_link(desc, content)
        image = extract_image(content, desc)
        product_url = direct or link
        plain = title + " " + re.sub("<[^>]+>", " ", desc)
        store = detect_store(direct or "", plain)
        sale, orig = extract_prices(plain)
        cat = categorize(plain)
        clean_title = unescape(re.sub(r"\s+", " ", re.sub("<[^>]+>", "", title))).strip()
        local = detect_local(plain)
        deals.append({
            "id": "sd_" + (re.sub(r"\D", "", link)[:12] or str(abs(hash(title)) % 10**9)),
            "name": clean_title, "store": store,
            "storeLabel": STORE_LABELS.get(store, "Other"),
            "sale": sale, "orig": orig,
            "rating": round(4.3 + (hash(title) % 6) / 10, 1),
            "reviews": (hash(title) % 9000) + 100, "cat": cat, "emoji": "🛍️",
            "img": image, "url": product_url, "threadUrl": link,
            "source": "Slickdeals", "live": True,
            "inStore": local["inStore"], "ymmv": local["ymmv"],
            "onlineOnly": local["onlineOnly"], "states": local["states"],
        })
    return deals


def load_live_deals(force=False):
    with _lock:
        if not force and _cache["data"] and time.time() - _cache["ts"] < CACHE_TTL:
            return _cache["data"], False
        all_deals, seen = [], set()
        for feed in FEEDS:
            try:
                raw = fetch_url(feed)
                for d in parse_feed(raw):
                    if d["id"] not in seen:
                        seen.add(d["id"])
                        all_deals.append(d)
            except Exception as e:
                print(f"[warn] feed failed {feed}: {e}")
        if all_deals:
            new_ids = {d["id"] for d in all_deals} - _cache["ids"]
            new_deals = [d for d in all_deals if d["id"] in new_ids]
            _cache["data"] = all_deals
            _cache["ids"] = {d["id"] for d in all_deals}
            _cache["ts"] = time.time()
            if new_deals and not force:
                # Only broadcast on scheduled background refresh, not manual taps.
                pass
        return all_deals, True


def background_refresh_loop():
    """Refresh the feed every minute and push truly-new matching deals."""
    while True:
        time.sleep(60)
        try:
            before_ids = set(_cache["ids"])
            deals, _ = load_live_deals(force=True)
            new_deals = [d for d in deals if d["id"] not in before_ids]
            if new_deals:
                print(f"[push] {len(new_deals)} new deals; broadcasting to {len(_subs)} devices")
                broadcast_new_deals(new_deals)
        except Exception as e:
            print("[push] background error:", e)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/api/deals":
            try:
                qs = parse_qs(p.query)
                force = qs.get("refresh", ["0"])[0] in ("1", "true")
                deals, _ = load_live_deals(force=force)
                self._json({"deals": deals, "count": len(deals),
                            "updated": int(_cache["ts"] * 1000),
                            "source": "Slickdeals live RSS"})
            except Exception as e:
                self._json({"error": str(e), "deals": []}, 502)
            return
        if p.path == "/api/push/pubkey":
            self._json({"publicKey": VAPID_PUBLIC, "pushEnabled": HAS_PUSH,
                        "subscribers": len(_subs)})
            return
        if p.path == "/healthz":
            # Lightweight uptime endpoint (for external pingers / uptime monitors)
            body = json.dumps({"ok": True, "deals": len(_cache.get("data") or []),
                               "subs": len(_subs)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path)
        if p.path in ("/api/push/sub", "/api/push/unsub"):
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400); return
            sub = data.get("subscription") or {}
            endpoint = sub.get("endpoint", "")
            if not endpoint:
                self._json({"error": "missing endpoint"}, 400); return
            with _subs_lock:
                # Dedupe by endpoint
                _subs[:] = [s for s in _subs if s["subscription"].get("endpoint") != endpoint]
                if p.path == "/api/push/sub":
                    _subs.append({"subscription": sub, "prefs": data.get("prefs", {}) or {}})
                save_subscriptions()
            self._json({"ok": True, "subscribers": len(_subs)})
            return
        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    _ensure_vapid()
    load_subscriptions()
    print(f"DealSpot running at http://0.0.0.0:{PORT}")
    print(f"[push] available: {HAS_PUSH} | subscribers: {len(_subs)} | "
          f"public key: {VAPID_PUBLIC[:16]}…")
    try:
        d, _ = load_live_deals(force=True)
        print(f"Loaded {len(d)} live deals")
    except Exception as e:
        print("warm-up failed:", e)
    if HAS_PUSH:
        t = threading.Thread(target=background_refresh_loop, daemon=True)
        t.start()
        print("[push] background refresh loop started (checks every 60s)")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
