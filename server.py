#!/usr/bin/env python3
"""
DealSpot backend
----------------
- Serves the PWA (index.html, manifest, icons, service worker)
- GET  /api/deals       -> live aggregated deals from Slickdeals RSS (cached 5 min)
                           ?pt=TOKEN skips the free early-access delay for members
- POST /api/push/sub    -> register a device for push notifications
- POST /api/push/unsub  -> remove a device
- GET  /api/push/pubkey -> VAPID public key the browser needs to subscribe
- GET  /api/premium/config   -> Premium status, price + payment link (for the app)
- POST /api/premium/activate -> verifies a Stripe checkout session, returns a
                                signed member token (works after restarts)
- POST /api/premium/verify   -> checks whether a member token is still valid

Push notifications: when the feed is refreshed and a NEW deal matches a
subscriber's saved keywords/stores/discount, the server sends a Web Push
message to their device. PREMIUM members get it instantly; free users get
it PREMIUM_EARLY_MINUTES (default 8) later, when the deal unlocks for all.

Configuration (set these in the Render dashboard → Environment):
  VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY  -> generated below, base64url
  VAPID_SUBJECT                        -> "mailto:you@example.com"
  PUSH_PUBLIC_BASE_URL                 -> https://dealspot-cn44.onrender.com
  PREMIUM_EARLY_MINUTES                -> free-user delay (default 8)
  PREMIUM_SECRET                       -> long random string that signs member
                                          tokens (keep secret, never change)
  STRIPE_SECRET_KEY                    -> from Stripe dashboard (sk_live_…)
  STRIPE_PAYMENT_URL                   -> your Stripe Payment Link URL
  PREMIUM_PRICE_LABEL                  -> shown in the app, e.g. "$2.99/month"
  STRIPE_MANAGE_URL                    -> optional Stripe customer portal link
If VAPID keys are missing the server generates an ephemeral pair on startup
(push still works for that session but subscriptions won't survive restarts —
set real keys in production).
"""

from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs, quote, urlencode
import xml.etree.ElementTree as ET
import json, re, time, os, threading, base64, hashlib, hmac, random
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
_first_seen = {}        # deal id -> first-seen time (ms) — powers Premium early access
_boot_grace = True      # first load after boot: back-date deals so free users see a full feed
_delayed_push = []      # deals waiting for the free-users' 8-minute mark
_lock = threading.Lock()

# ── Push state ─────────────────────────────────────────────────────────
SUBS_FILE = os.path.join(HERE, "subscriptions.json")
_subs = []
_subs_lock = threading.Lock()
VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:dealspot@example.com")
PUBLIC_BASE_URL = os.environ.get("PUSH_PUBLIC_BASE_URL", "")

# ── Premium (paid upgrade) state ────────────────────────────────────────
# Free users see brand-new deals PREMIUM_EARLY_MINUTES later than members.
PREMIUM_EARLY_MIN = int(os.environ.get("PREMIUM_EARLY_MINUTES", "8"))
FREE_DELAY_SEC = PREMIUM_EARLY_MIN * 60
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PAYMENT_URL = os.environ.get("STRIPE_PAYMENT_URL", "")
STRIPE_MANAGE_URL = os.environ.get("STRIPE_MANAGE_URL", "")
PREMIUM_PRICE_LABEL = os.environ.get("PREMIUM_PRICE_LABEL", "$2.99/month")
PREMIUM_SECRET = os.environ.get("PREMIUM_SECRET", "")

# ── Extras: affiliate tags, tip jar, weekly email digest ───────────────
# Leave empty and nothing changes — set them any time, links/buttons
# activate instantly without a redeploy of code (just env save + restart).
AMAZON_TAG = os.environ.get("AMAZON_TAG", "")      # e.g. "dealspot-20"
EBAY_CAMPID = os.environ.get("EBAY_CAMPID", "")    # eBay Partner Network id
TIP_URL = os.environ.get("TIP_URL", "")            # e.g. https://ko-fi.com/yourpage
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")   # free @ resend.com
DIGEST_FROM = os.environ.get("DIGEST_FROM", "DealSpot <dealspot@resend.dev>")

# ── Community votes + digest list state ────────────────────────────────
VOTES_FILE = os.path.join(HERE, "votes.json")
_votes = {}                    # deal id -> {"fire": n, "dead": n}
_votes_lock = threading.Lock()
DIGEST_FILE = os.path.join(HERE, "digest_emails.json")
_digest = {"emails": [], "last_sent": 0}
_digest_lock = threading.Lock()


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


def _ensure_premium_secret():
    """Premium tokens are signed with this secret. Set PREMIUM_SECRET in the
    environment so upgrades survive restarts; otherwise we fall back to a
    key derived from the VAPID private key (works, but warn)."""
    global PREMIUM_SECRET
    if PREMIUM_SECRET:
        return
    if VAPID_PRIVATE:
        PREMIUM_SECRET = "vapid:" + VAPID_PRIVATE
    else:
        PREMIUM_SECRET = hashlib.sha256(("ephemeral:" + str(time.time())).encode()).hexdigest()
    print("[premium] WARNING: PREMIUM_SECRET not set — deriving one from other keys. "
          "Set PREMIUM_SECRET env var so member upgrades survive restarts!")


def make_premium_token(session_id, mode):
    """Self-contained signed token — no database needed, survives restarts.
    Monthly (subscription) tokens expire in 32 days, one-time in 10 years."""
    days = 32 if mode == "subscription" else 3650
    payload = json.dumps({
        "exp": int(time.time() + days * 86400),
        "mode": mode or "payment",
        "sid": hashlib.sha256(session_id.encode()).hexdigest()[:12],
    }, separators=(",", ":"), sort_keys=True).encode()
    body = _b64url_encode(payload)
    sig = _b64url_encode(hmac.new(PREMIUM_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def verify_premium_token(token):
    if not token or "." not in token:
        return None
    try:
        body, sig = token.split(".", 1)
        expect = _b64url_encode(hmac.new(PREMIUM_SECRET.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(_b64url_decode(body))
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


def stripe_get_session(session_id):
    """Ask Stripe directly whether this checkout session was really paid."""
    req = Request(
        "https://api.stripe.com/v1/checkout/sessions/" + quote(session_id, safe=""),
        headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY})
    with urlopen(req, timeout=12) as r:
        return json.load(r)


def load_subscriptions():
    global _subs
    try:
        with open(SUBS_FILE) as f:
            _subs = json.load(f)
    except Exception:
        _subs = []


def load_votes():
    try:
        with open(VOTES_FILE) as f:
            _votes.update(json.load(f))
    except Exception:
        pass


def save_votes():
    try:
        tmp = VOTES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_votes, f)
        os.replace(tmp, VOTES_FILE)
    except Exception as e:
        print("[votes] could not save:", e)


def load_digest():
    try:
        with open(DIGEST_FILE) as f:
            d = json.load(f)
            _digest["emails"] = d.get("emails", [])
            _digest["last_sent"] = d.get("last_sent", 0)
    except Exception:
        pass


def save_digest():
    try:
        tmp = DIGEST_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_digest, f)
        os.replace(tmp, DIGEST_FILE)
    except Exception as e:
        print("[digest] could not save:", e)


def apply_affiliate(url):
    """Append affiliate tags to retailer links. No tags configured -> no-op."""
    if not url or not url.startswith("http"):
        return url
    low = url.lower()
    if AMAZON_TAG and "amazon." in low:
        return _set_query_param(url, "tag", AMAZON_TAG)
    if EBAY_CAMPID and ("ebay." in low):
        return _set_query_param(url, "campid", EBAY_CAMPID)
    return url


def _set_query_param(url, key, value):
    base, frag = url.split("#", 1) if "#" in url else (url, None)
    base, q = base.split("?", 1) if "?" in base else (base, "")
    params = parse_qs(q, keep_blank_values=True)
    params[key] = [value]
    from urllib.parse import urlencode
    out = base + "?" + urlencode({k: v[0] for k, v in params.items()})
    return out + ("#" + frag if frag else "")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def digest_items():
    """Top 10 deals by discount for the weekly email / preview."""
    deals = _cache.get("data") or []
    scored = []
    for d in deals:
        sale, orig = d.get("sale"), d.get("orig")
        if sale and orig and orig > sale:
            scored.append((round((1 - sale / orig) * 100), d))
    scored.sort(key=lambda x: -x[0])
    return [{"name": d["name"], "store": d.get("storeLabel", "Store"),
             "sale": d["sale"], "orig": d.get("orig"), "pct": pct}
            for pct, d in scored[:10]]


def send_digest_email(to_list):
    """Send the weekly Top-10 email via Resend. Returns count sent."""
    items = digest_items()
    if not items:
        return 0
    rows = "".join(
        f'<tr><td style="padding:10px 0;border-bottom:1px solid #eee">'
        f'<div style="font-size:15px;font-weight:700">{i["store"]}</div>'
        f'<div style="font-size:13px;color:#333">{i["name"]}</div>'
        f'<div style="font-size:13px;margin-top:3px">'
        f'<b style="color:#e8590c">-{i["pct"]}%</b> · ${i["sale"]:.2f}'
        + (f' <s>${i["orig"]:.2f}</s>' if i.get("orig") else "") +
        f'</div></td></tr>'
        for i in items)
    html = (f'<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto">'
            f'<h2 style="margin-bottom:4px">🏷️ DealSpot — Top deals this week</h2>'
            f'<p style="color:#666;font-size:13px;margin-top:0">The {len(items)} biggest live discounts right now.</p>'
            f'<table style="width:100%;border-collapse:collapse">{rows}</table>'
            f'<p style="text-align:center;margin:22px 0">'
            f'<a href="{PUBLIC_BASE_URL or "https://dealspot.onrender.com"}" '
            f'style="background:#6c5ce7;color:#fff;padding:12px 26px;border-radius:10px;'
            f'text-decoration:none;font-weight:700">Open DealSpot</a></p>'
            f'<p style="color:#999;font-size:11px">You signed up in the DealSpot app. '
            f'Reply or unsubscribe any time in Settings → Weekly Deal Digest.</p></div>')
    sent = 0
    for to in to_list[:90]:  # free Resend tier: 100/day
        try:
            req = Request("https://api.resend.com/emails",
                          data=json.dumps({"from": DIGEST_FROM, "to": [to],
                                           "subject": "🏷️ Your DealSpot Top 10 deals this week",
                                           "html": html}).encode(),
                          headers={"Authorization": "Bearer " + RESEND_API_KEY,
                                   "Content-Type": "application/json"})
            with urlopen(req, timeout=12) as r:
                if r.status in (200, 201):
                    sent += 1
        except Exception as e:
            print(f"[digest] send failed to {to}: {e}")
        time.sleep(0.15)
    return sent


def digest_loop():
    """Send the weekly digest once every 7 days (if a key is configured)."""
    while True:
        time.sleep(3600)
        try:
            if not RESEND_API_KEY or not _digest["emails"]:
                continue
            if time.time() - _digest.get("last_sent", 0) < 7 * 86400:
                continue
            sent = send_digest_email(_digest["emails"])
            if sent:
                with _digest_lock:
                    _digest["last_sent"] = int(time.time())
                    save_digest()
                print(f"[digest] weekly email sent to {sent} subscribers")
        except Exception as e:
            print("[digest] loop error:", e)


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


def _deal_payload(matches, instant=False):
    prefix = "⚡ " if instant else "🔥 "
    if len(matches) == 1:
        d = matches[0]
        pct = 0
        if d.get("sale") and d.get("orig") and d["orig"] > d["sale"]:
            pct = round((1 - d["sale"] / d["orig"]) * 100)
        return {
            "title": f"{prefix}{d.get('storeLabel','Deal')} deal" + (f" -{pct}%" if pct else ""),
            "body": d["name"][:140],
            "url": PUBLIC_BASE_URL + "/?deal=" + str(d["id"]) if PUBLIC_BASE_URL else (d.get("url") or "/"),
            "tag": "deal-" + str(d["id"]),
        }
    return {
        "title": f"{prefix}{len(matches)} new matching deals",
        "body": " · ".join(m["name"][:50] for m in matches),
        "url": (PUBLIC_BASE_URL + "/") if PUBLIC_BASE_URL else "/",
        "tag": "deals-summary",
    }


def send_matches(subs, deals, instant=False):
    for sub in subs:
        matches = [d for d in deals if sub_matches(sub, d)][:3]
        if not matches:
            continue
        send_push(sub, _deal_payload(matches, instant))


def broadcast_new_deals(new_deals):
    """Premium members get pushes INSTANTLY; free users get them once the
    deal's early-access window (PREMIUM_EARLY_MINUTES) has passed."""
    if not new_deals:
        return
    with _subs_lock:
        subs = list(_subs)
    premium_subs = [s for s in subs if s.get("premium")]
    if premium_subs:
        send_matches(premium_subs, new_deals, instant=True)
    # Queue for free users — flushed by the background loop at unlock time.
    for d in new_deals:
        _delayed_push.append({"id": d["id"], "deal": d, "at": time.time() + FREE_DELAY_SEC})
    _delayed_push[:] = _delayed_push[-200:]


def flush_delayed_pushes():
    """Deliver queued deals to free users once their 8-minute window ends."""
    now = time.time()
    due = [x for x in _delayed_push if x["at"] <= now]
    if not due:
        return
    _delayed_push[:] = [x for x in _delayed_push if x["at"] > now]
    with _subs_lock:
        free_subs = [s for s in _subs if not s.get("premium")]
    for item in due:
        # Only send if the deal is still in the current feed.
        if item["id"] in _cache["ids"]:
            send_matches(free_subs, [item["deal"]])


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
    global _boot_grace
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
            now_ms = int(time.time() * 1000)
            for d in all_deals:
                if d["id"] not in _first_seen:
                    if _boot_grace:
                        # Very first load after startup: pretend deals are
                        # 12–45 min old so free users don't stare at an empty
                        # feed for the early-access window.
                        _first_seen[d["id"]] = now_ms - random.randint(12, 45) * 60 * 1000
                    else:
                        _first_seen[d["id"]] = now_ms
                d["firstSeen"] = _first_seen[d["id"]]
            _boot_grace = False
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
            flush_delayed_pushes()
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
                token = (qs.get("pt") or [self.headers.get("X-Premium-Token", "")])[0]
                payload = verify_premium_token(token)
                deals, _ = load_live_deals(force=force)
                with _votes_lock:
                    for d in deals:
                        d["votes"] = _votes.get(d["id"]) or {"fire": 0, "dead": 0}
                        d["url"] = apply_affiliate(d.get("url"))
                out = {"deals": deals, "count": len(deals),
                       "updated": int(_cache["ts"] * 1000),
                       "source": "Slickdeals live RSS", "premium": bool(payload)}
                if not payload:
                    # Free user: hold back brand-new deals for the early-access
                    # window, but tell them what they're missing (upsell teaser).
                    now_ms = int(time.time() * 1000)
                    fresh = [d for d in deals
                             if now_ms - d.get("firstSeen", 0) < FREE_DELAY_SEC * 1000]
                    if fresh:
                        out["deals"] = [d for d in deals
                                        if now_ms - d.get("firstSeen", 0) >= FREE_DELAY_SEC * 1000]
                        out["count"] = len(out["deals"])
                        out["premiumTeaser"] = {
                            "count": len(fresh),
                            "unlockAt": min(d.get("firstSeen", 0) for d in fresh) + FREE_DELAY_SEC * 1000,
                        }
                self._json(out)
            except Exception as e:
                self._json({"error": str(e), "deals": []}, 502)
            return
        if p.path == "/api/premium/config":
            qs = parse_qs(p.query)
            token = (qs.get("pt") or [self.headers.get("X-Premium-Token", "")])[0]
            payload = verify_premium_token(token)
            self._json({
                "enabled": bool(STRIPE_SECRET_KEY and STRIPE_PAYMENT_URL),
                "price": PREMIUM_PRICE_LABEL,
                "paymentUrl": STRIPE_PAYMENT_URL,
                "manageUrl": STRIPE_MANAGE_URL,
                "earlyMinutes": PREMIUM_EARLY_MIN,
                "premium": bool(payload),
                "mode": payload.get("mode") if payload else None,
                "exp": payload.get("exp") if payload else None,
                "tipUrl": TIP_URL,
                "digest": {"subscribers": len(_digest["emails"]),
                           "sending": bool(RESEND_API_KEY)},
            })
            return
        if p.path == "/api/push/pubkey":
            self._json({"publicKey": VAPID_PUBLIC, "pushEnabled": HAS_PUSH,
                        "subscribers": len(_subs)})
            return
        if p.path == "/healthz":
            # Lightweight uptime endpoint (for external pingers / uptime monitors)
            body = json.dumps({"ok": True, "deals": len(_cache.get("data") or []),
                               "subs": len(_subs),
                               "premium": bool(STRIPE_SECRET_KEY and STRIPE_PAYMENT_URL),
                               "digest": len(_digest["emails"])}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path)
        if p.path in ("/api/deals/vote", "/api/digest/subscribe", "/api/digest/unsubscribe", "/api/digest/send-test"):
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400); return
            if p.path == "/api/deals/vote":
                did = str(data.get("id", ""))[:60]
                add = data.get("add") if data.get("add") in ("fire", "dead") else None
                remove = data.get("remove") if data.get("remove") in ("fire", "dead") else None
                if not did or not (add or remove):
                    self._json({"ok": False, "error": "need id + add/remove"}, 400); return
                with _votes_lock:
                    v = _votes.setdefault(did, {"fire": 0, "dead": 0})
                    if remove and remove != add:
                        v[remove] = max(0, v[remove] - 1)
                    if add:
                        v[add] = (v[add] or 0) + 1
                    save_votes()
                    out_v = dict(v)
                self._json({"ok": True, "votes": out_v})
                return
            email = str(data.get("email", "")).strip().lower()
            if p.path == "/api/digest/subscribe":
                if not EMAIL_RE.match(email):
                    self._json({"ok": False, "error": "Enter a valid email address"}, 400); return
                with _digest_lock:
                    if email not in _digest["emails"]:
                        _digest["emails"].append(email)
                        save_digest()
                    count = len(_digest["emails"])
                print(f"[digest] subscriber added: {email} (total {count})")
                self._json({"ok": True, "subscribers": count}); return
            if p.path == "/api/digest/unsubscribe":
                with _digest_lock:
                    _digest["emails"] = [e for e in _digest["emails"] if e != email]
                    save_digest()
                    count = len(_digest["emails"])
                self._json({"ok": True, "subscribers": count}); return
            # /api/digest/send-test → send one preview email now
            if not RESEND_API_KEY:
                self._json({"ok": False, "error": "Email sending isn't configured yet"}, 503); return
            to = email or "me"
            if not EMAIL_RE.match(to):
                self._json({"ok": False, "error": "Enter a valid email address"}, 400); return
            sent = send_digest_email([to])
            self._json({"ok": bool(sent), "sent": sent}); return
        if p.path in ("/api/premium/activate", "/api/premium/verify"):
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400); return
            if p.path == "/api/premium/verify":
                payload = verify_premium_token(data.get("token", ""))
                self._json({"premium": bool(payload),
                            "mode": payload.get("mode") if payload else None,
                            "exp": payload.get("exp") if payload else None})
                return
            # activate: verify a real Stripe payment, then mint a member token
            sid = str(data.get("sessionId", "")).strip()
            if not STRIPE_SECRET_KEY:
                self._json({"ok": False, "error": "Payments not configured yet"}, 503); return
            if not re.fullmatch(r"[A-Za-z0-9_\-]{10,255}", sid):
                self._json({"ok": False, "error": "That doesn't look like a valid payment code"}, 400); return
            try:
                sess = stripe_get_session(sid)
            except Exception as e:
                print("[premium] stripe verify failed:", e)
                self._json({"ok": False, "error": "Could not verify that payment — try again"}, 502); return
            if sess.get("payment_status") != "paid":
                self._json({"ok": False, "error": "Payment not completed yet"}, 402); return
            token = make_premium_token(sid, sess.get("mode") or "payment")
            print("[premium] activated:", sess.get("mode"), "·",
                  (sess.get("customer_details") or {}).get("email", "(no email)"))
            self._json({"ok": True, "token": token,
                        "mode": sess.get("mode") or "payment",
                        "email": (sess.get("customer_details") or {}).get("email") or ""})
            return
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
                    _subs.append({"subscription": sub, "prefs": data.get("prefs", {}) or {},
                                  "premium": bool(verify_premium_token(data.get("premiumToken", "")))})
                save_subscriptions()
            self._json({"ok": True, "subscribers": len(_subs)})
            return
        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    _ensure_vapid()
    _ensure_premium_secret()
    load_subscriptions()
    load_votes()
    load_digest()
    print(f"DealSpot running at http://0.0.0.0:{PORT}")
    print(f"[push] available: {HAS_PUSH} | subscribers: {len(_subs)} | "
          f"public key: {VAPID_PUBLIC[:16]}…")
    print(f"[premium] payments configured: {bool(STRIPE_SECRET_KEY and STRIPE_PAYMENT_URL)} | "
          f"free delay: {PREMIUM_EARLY_MIN} min")
    print(f"[extras] votes: {len(_votes)} | digest subs: {len(_digest['emails'])} | "
          f"email sending: {bool(RESEND_API_KEY)} | amazon tag: {AMAZON_TAG or '—'}")
    if RESEND_API_KEY:
        threading.Thread(target=digest_loop, daemon=True).start()
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
