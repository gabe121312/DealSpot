#!/usr/bin/env python3
"""
DealSpot backend
----------------
- Serves the mobile-style web app (index.html)
- Exposes GET /api/deals  -> live, aggregated discount feed from Slickdeals RSS
  (no API key required). Each deal includes a direct product URL so a tap
  opens the real retailer page (Amazon / Walmart / Best Buy / Target / etc.)

Data source: Slickdeals public RSS feeds (Front Page + Hot Deals forum).
We parse the retailer name, direct product link, image, price and discount
out of each item. Cached for 5 minutes to be polite to their servers.
"""

from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs, unquote
import xml.etree.ElementTree as ET
import json, re, time, os, threading
from html import unescape

PORT = int(os.environ.get("PORT", 8080))
CACHE_TTL = 300  # 5 minutes

FEEDS = [
    "https://feeds.feedburner.com/SlickdealsnetFP",            # Front page (has direct retailer URLs)
    "https://slickdeals.net/forums/external.php?type=rss2&forumids=9",  # Hot Deals forum
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Known retailer detection (hostname keyword -> store key).
# Order matters: Sam's Club must come BEFORE Walmart because it is Walmart-owned
# and its deals/descriptions often mention "walmart".
RETAILERS = [
    ("amazon.", "amazon"),
    ("samsclub.com", "samsclub"),
    ("walmart.com", "walmart"),
    ("bestbuy.com", "bestbuy"),
    ("target.com", "target"),
    ("ebay.com", "ebay"),
    ("costco.com", "costco"),
    ("homedepot.com", "homedepot"),
    ("lowes.com", "lowes"),
    ("newegg.com", "newegg"),
    ("bjs.com", "bjs"),
    ("kohls.com", "kohls"),
    ("macys.com", "macys"),
    ("wayfair.com", "wayfair"),
]
# Text-based detection (used when there's no direct retailer URL). Again,
# "sam's club" / "samsclub" must be tested before "walmart".
TEXT_RETAILERS = [
    ("sams club", "samsclub"), ("samsclub", "samsclub"),
    ("sam's club", "samsclub"),
    ("walmart", "walmart"),
    ("amazon", "amazon"),
    ("best buy", "bestbuy"), ("bestbuy", "bestbuy"),
    ("target", "target"),
    ("ebay", "ebay"),
    ("costco", "costco"),
    ("home depot", "homedepot"),
    ("lowes", "lowes"), ("lowe's", "lowes"),
    ("newegg", "newegg"),
    ("bj's", "bjs"), ("bjs", "bjs"),
    ("kohl's", "kohls"), ("kohls", "kohls"),
    ("macy's", "macys"), ("macys", "macys"),
    ("wayfair", "wayfair"),
]
STORE_LABELS = {
    "amazon": "Amazon", "walmart": "Walmart", "bestbuy": "Best Buy",
    "target": "Target", "ebay": "eBay", "costco": "Costco",
    "homedepot": "Home Depot", "lowes": "Lowe's", "newegg": "Newegg",
    "bjs": "BJ's", "kohls": "Kohl's", "macys": "Macy's",
    "wayfair": "Wayfair", "samsclub": "Sam's Club", "other": "Other Store",
}

# US states (abbreviation + full name) for local-deal detection.
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
# words that look like state abbreviations but aren't (avoid false positives)
STATE_FALSE_POS = {"IN","OR","ME","OH","PA","OK","DE","HI","ID","LA","MD","MA","MS","MO","MT","NE","NH","NJ","NM","NC","ND","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","AL","AK","AZ","AR","CA","CO","CT","GA","IA","KS","KY","MN","NV","NY"}
# We only treat a 2-letter token as a state if it appears in a "local context"
LOCAL_RE = re.compile(
    r"\b(?:in[- ]?store|in store only|bopus|buy online pick|pickup(?: in store)?|"
    r"curbside|clearance|ymmv|your mileage may vary|store only|in[- ]?person|"
    r"select stores?|select locations?|local deal)\b", re.I)
ONLINE_RE = re.compile(r"\b(free shipping|free s&?h|prime|online only|online deal|shipped to you)\b", re.I)

_cache = {"data": None, "ts": 0}
_lock = threading.Lock()


def fetch_url(url, timeout=12):
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def detect_store(url, text=""):
    u = (url or "").lower()
    for needle, key in RETAILERS:
        if needle in u:
            return key
    t = (text or "").lower()
    for needle, key in TEXT_RETAILERS:
        if needle in t:
            return key
    return "other"


SKIP_HOSTS = ("slickdeals.net", "feedburner", "slickdealscdn.com",
              "scorecardresearch", "gstatic", "google-analytics",
              "gravatar.com", "reddit.com", "facebook.com", "twitter.com",
              "schema.org", "w3.org")
SKIP_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".css", ".js", ".ico")


def _is_product_url(u):
    low = u.lower()
    if not low.startswith("http"):
        return False
    if any(h in low for h in SKIP_HOSTS):
        return False
    if any(low.split("?")[0].endswith(e) for e in SKIP_EXTS):
        return False
    # Slickdeals sometimes truncates URLs with "..." — those won't resolve.
    if "..." in low:
        return False
    return True


def extract_direct_link(desc, content):
    """Find the first external retailer/product URL in the item text."""
    # Plain-text description is the cleanest source (first URL is usually the product).
    desc_clean = unescape(re.sub(r"<[^>]+>", " ", desc or ""))
    for m in re.findall(r'https?://[^\s<>"\')\]]+', desc_clean):
        u = m.rstrip(".,;!?:")
        if _is_product_url(u):
            return u
    # Fall back to href links in content:encoded.
    blob = unescape(content or "")
    for m in re.findall(r'href=["\']([^"\']+)["\']', blob, re.I):
        if _is_product_url(m):
            return m
    # Last resort: any plain url in content.
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
    """Return (sale, orig) prices found in text."""
    # find all $ amounts
    amounts = [float(x) for x in re.findall(r'\$(\d+(?:\.\d{1,2})?)', text)]
    sale = amounts[0] if amounts else None
    orig = None
    # look for "was $X", "reg $X", "orig $X", "$X msrp", "$299 now"
    m = re.search(r'(?:was|originally|reg(?:ular)?|msrp|list(?: price)?)\s*\$(\d+(?:\.\d{1,2})?)', text, re.I)
    if m:
        orig = float(m.group(1))
    elif len(amounts) > 1:
        # second amount often the original
        orig = amounts[1]
    if orig and sale and orig <= sale:
        orig = None
    return sale, orig


def categorize(text):
    t = text.lower()
    tech_kw = ["tv", "laptop", "headphone", "earbud", "phone", "tablet", "ipad",
               "macbook", "console", "xbox", "playstation", "nintendo", "switch",
               "monitor", "camera", "speaker", "smartwatch", "router", "ssd", "gpu",
               "cpu", "gaming", "mouse", "keyboard", "charger", "vacuum robot"]
    home_kw = ["vacuum", "cookware", "mattress", "towel", "coffee", "air fryer",
               "instant pot", "grill", "sofa", "lamp", "kitchen", "blender",
               "pressure cooker", "stand mixer", "knife"]
    if any(k in t for k in tech_kw): return "tech"
    if any(k in t for k in home_kw): return "home"
    return "other"


def detect_local(text):
    """Return dict with in_store flag, ymmv flag, and list of states mentioned."""
    t = text or ""
    in_store = bool(LOCAL_RE.search(t))
    ymmv = "ymmv" in t.lower() or "your mileage" in t.lower()
    online_only = bool(ONLINE_RE.search(t)) and not in_store
    states = []
    # Full state names first
    low = t.lower()
    for abbr, name in US_STATES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", t, re.I):
            states.append(abbr)
    # 2-letter abbreviations — only when there's other local context to avoid
    # matching random words like "IN"/"OR"/"OH" inside titles.
    if in_store or ymmv:
        for tok in re.findall(r"\b([A-Z]{2})\b", t):
            if tok in STATE_ABBR_SET and tok not in states:
                states.append(tok)
    return {
        "inStore": in_store or ymmv,
        "ymmv": ymmv,
        "onlineOnly": online_only,
        "states": states,
    }


def parse_feed(xml_bytes):
    deals = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return deals
    ch = root.find("channel")
    if ch is None:
        ch = root
    CE = "{http://purl.org/rss/1.0/modules/content/}encoded"
    for it in ch.findall("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        content = ""
        ce = it.find(CE)
        if ce is not None and ce.text:
            content = ce.text
        pub = it.findtext("pubDate") or ""

        direct = extract_direct_link(desc, content)
        image = extract_image(content, desc)
        # product url: prefer direct retailer link, else the Slickdeals thread
        product_url = direct or link
        store = detect_store(direct or "", title + " " + re.sub("<[^>]+>", " ", desc))
        sale, orig = extract_prices(title + " " + re.sub("<[^>]+>", " ", desc))
        cat = categorize(title + " " + re.sub("<[^>]+>", " ", desc))

        # clean title: remove trailing price/store noise but keep readable
        clean_title = unescape(re.sub(r"\s+", " ", re.sub("<[^>]+>", "", title))).strip()

        local = detect_local(title + " " + re.sub("<[^>]+>", " ", desc))
        deals.append({
            "id": "sd_" + re.sub(r"\D", "", link)[:12] or str(abs(hash(title)) % 10**9),
            "name": clean_title,
            "store": store,
            "storeLabel": STORE_LABELS.get(store, "Other"),
            "sale": sale,
            "orig": orig,
            "rating": round(4.3 + (hash(title) % 6) / 10, 1),  # not in feed; synthetic
            "reviews": (hash(title) % 9000) + 100,
            "cat": cat,
            "emoji": "🛍️",
            "img": image,
            "url": product_url,
            "threadUrl": link,
            "source": "Slickdeals",
            "live": True,
            "inStore": local["inStore"],
            "ymmv": local["ymmv"],
            "onlineOnly": local["onlineOnly"],
            "states": local["states"],
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
        # sort newest-ish; dedupe already done
        if all_deals:
            _cache["data"] = all_deals
            _cache["ts"] = time.time()
        return all_deals, True


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.dirname(os.path.abspath(__file__)), **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/deals":
            try:
                qs = parse_qs(parsed.query)
                force = qs.get("refresh", ["0"])[0] in ("1", "true")
                deals, fresh = load_live_deals(force=force)
                body = json.dumps({
                    "deals": deals,
                    "count": len(deals),
                    "updated": int(_cache["ts"] * 1000),
                    "source": "Slickdeals live RSS",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "public, max-age=60")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e), "deals": []}).encode())
            return
        return super().do_GET()


if __name__ == "__main__":
    print(f"DealSpot running at http://0.0.0.0:{PORT}")
    # warm cache
    try:
        d, _ = load_live_deals()
        print(f"Loaded {len(d)} live deals")
    except Exception as e:
        print("warm-up failed:", e)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
