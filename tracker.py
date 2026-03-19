“””
Flipkart PS5 Price Tracker Bot
Monitors listed price + instant card offers and sends Telegram alerts.
Two scenarios:

1. Listed price drops below TARGET
1. Effective price (after instant card offers) drops below TARGET
   “””

import os
import sys
import json
import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ─────────────────────────────────────────────

# LOGGING

# ─────────────────────────────────────────────

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s [%(levelname)s] %(message)s”
)
log = logging.getLogger(**name**)

# ─────────────────────────────────────────────

# CONFIG

# ─────────────────────────────────────────────

BOT_TOKEN        = os.environ.get(“TELEGRAM_BOT_TOKEN”, “”)
CHAT_ID          = os.environ.get(“TELEGRAM_CHAT_ID”, “”)
PRODUCT_URL      = os.environ.get(“FLIPKART_URL”, “”)
SCRAPER_API_KEY  = os.environ.get(“SCRAPER_API_KEY”, “”)

TARGET           = 44900
STATE_FILE       = “state.json”
REMINDER_MINUTES = 30
PRICE_MIN        = 40_000   # PS5 will never be below this legitimately
PRICE_MAX        = 80_000   # PS5 will never be above this legitimately

SCRAPER_API_URL  = “https://api.scraperapi.com”

# ─────────────────────────────────────────────

# STARTUP VALIDATION

# ─────────────────────────────────────────────

def validate_config():
errors = []

```
if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{35,}', BOT_TOKEN):
    errors.append("TELEGRAM_BOT_TOKEN format looks invalid.")

if not re.fullmatch(r'-?\d+', CHAT_ID.strip()):
    errors.append("TELEGRAM_CHAT_ID must be numeric.")

try:
    parsed = urlparse(PRODUCT_URL)
    if parsed.scheme != "https":
        errors.append("FLIPKART_URL must use HTTPS.")
    if "flipkart.com" not in parsed.netloc:
        errors.append("FLIPKART_URL must be from flipkart.com.")
except Exception:
    errors.append("FLIPKART_URL is not a valid URL.")

if len(SCRAPER_API_KEY.strip()) < 10:
    errors.append("SCRAPER_API_KEY looks invalid or too short.")

if errors:
    for e in errors:
        log.error(f"Config error: {e}")
    sys.exit(1)

log.info("Config validation passed.")
```

# ─────────────────────────────────────────────

# STATE MANAGEMENT

# ─────────────────────────────────────────────

def load_state():
default = {
“scenario1”: {“count”: 0, “last_alert_ts”: None},
“scenario2”: {“count”: 0, “last_alert_ts”: None},
}
if Path(STATE_FILE).exists():
try:
with open(STATE_FILE) as f:
loaded = json.load(f)
for key in (“scenario1”, “scenario2”):
if key not in loaded:
raise ValueError(f”Missing key: {key}”)
if “count” not in loaded[key] or “last_alert_ts” not in loaded[key]:
raise ValueError(f”Malformed state for {key}”)
if not isinstance(loaded[key][“count”], int):
raise ValueError(f”count must be int for {key}”)
return loaded
except Exception as e:
log.warning(f”state.json invalid ({e}), resetting to default.”)
return default

def save_state(state):
with open(STATE_FILE, “w”) as f:
json.dump(state, f, indent=2)
log.info(“State saved.”)

# ─────────────────────────────────────────────

# SCRAPING

# ─────────────────────────────────────────────

def fetch_page(url, render=False):
try:
payload = {
“api_key”: SCRAPER_API_KEY,
“url”: url,
“render”: “true” if render else “false”,
“country_code”: “in”,
}
resp = requests.get(SCRAPER_API_URL, params=payload, timeout=90)
if resp.status_code == 200:
log.info(f”Page fetched via ScraperAPI (render={render}).”)
return resp.text
else:
log.error(f”ScraperAPI returned status {resp.status_code}”)
return None
except Exception as e:
log.error(f”Failed to fetch page: {type(e).**name**}”)
return None

def extract_price(soup):
# Strategy 1: Known CSS selectors
selectors = [
“._30jeq3”, “.Nx9bqj”, “._16Jk6d”, “._1vC4OE”,
“.CEmiEU”, “._25b18c”, “.CxhGGd”, “._3qQ9m1”,
]
for sel in selectors:
el = soup.select_one(sel)
if el:
raw = el.get_text(strip=True).replace(”\u20b9”, “”).replace(”,”, “”).strip()
try:
price = int(float(raw))
if PRICE_MIN <= price <= PRICE_MAX:
log.info(f”Price found via CSS selector: {sel}”)
return price
except ValueError:
continue

```
# Strategy 2: Rupee symbol search
for el in soup.find_all(string=re.compile(r'\u20b9\s*[\d,]{4,7}')):
    match = re.search(r'\u20b9\s*([\d,]+)', str(el))
    if match:
        try:
            price = int(match.group(1).replace(",", ""))
            if PRICE_MIN <= price <= PRICE_MAX:
                log.info("Price found via rupee symbol search.")
                return price
        except ValueError:
            continue

# Strategy 3: JSON-LD structured data
for script in soup.find_all("script", type="application/ld+json"):
    try:
        data = json.loads(script.string)
        items = data if isinstance(data, list) else [data]
        for item in items:
            offers = item.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price_raw = offers.get("price") or item.get("price")
            if price_raw:
                price = int(float(str(price_raw).replace(",", "")))
                if PRICE_MIN <= price <= PRICE_MAX:
                    log.info("Price found via JSON-LD.")
                    return price
    except Exception:
        continue

# Strategy 4: Meta tags
for meta in soup.find_all("meta"):
    content = meta.get("content", "").strip()
    prop = (meta.get("property", "") or meta.get("name", "")).lower()
    if "price" in prop and re.fullmatch(r'\d{4,7}', content):
        try:
            price = int(content)
            if PRICE_MIN <= price <= PRICE_MAX:
                log.info("Price found via meta tag.")
                return price
        except ValueError:
            continue

log.warning("Could not find price with any strategy.")
return None
```

def extract_offers(soup):
offers = []
seen_pairs = set()

```
bank_pattern = re.compile(
    r'(HDFC|SBI|ICICI|Axis|Kotak|RBL|IDFC|IndusInd|Yes Bank|'
    r'Citi|HSBC|Amex|American Express|BOB|Bank of Baroda)',
    re.IGNORECASE
)

offer_containers = soup.select(
    "._2Tpdn3, .offer-wrap, ._3xFOBe, ._2AkmmA, .TVhoEJ, "
    "li._7eSDEz, ._1LKTO3, .offer-item, ._3HMbXn, "
    "._2xRNHi, .fMghEO, ._2LHPH5"
)
all_text_blocks = soup.find_all(string=bank_pattern)
raw_texts = [el.get_text(" ", strip=True) for el in offer_containers]
raw_texts += [str(t) for t in all_text_blocks]

seen_texts = set()
for text in raw_texts:
    if not text or text in seen_texts:
        continue
    seen_texts.add(text)

    if re.search(r'\bEMI\b', text, re.IGNORECASE) and \
       not re.search(r'instant|cashback|\boff\b', text, re.IGNORECASE):
        continue

    match = re.search(
        r'(?:\u20b9|Rs\.?)\s*([\d,]+)\s*(?:off|instant|discount)',
        text, re.IGNORECASE
    )
    if not match:
        match = re.search(
            r'instant.*?(?:\u20b9|Rs\.?)\s*([\d,]+)',
            text, re.IGNORECASE
        )
    if not match:
        continue

    try:
        amount = int(match.group(1).replace(",", ""))
    except ValueError:
        continue

    if amount < 500:
        continue

    bank_match = re.search(bank_pattern, text)
    bank = bank_match.group(1).upper() if bank_match else "Bank"
    card_type = "Card"
    if re.search(r'credit', text, re.IGNORECASE):
        card_type = "Credit Card"
    elif re.search(r'debit', text, re.IGNORECASE):
        card_type = "Debit Card"

    key = (bank, amount)
    if key not in seen_pairs:
        seen_pairs.add(key)
        offers.append({"bank": bank, "card_type": card_type, "discount": amount})

return sorted(offers, key=lambda x: x["discount"], reverse=True)
```

# ─────────────────────────────────────────────

# TELEGRAM

# ─────────────────────────────────────────────

def send_telegram(message, retries=3):
tg_url = “https://api.telegram.org/bot” + BOT_TOKEN + “/sendMessage”
payload = {
“chat_id”: CHAT_ID,
“text”: message,
“parse_mode”: “HTML”,
“disable_web_page_preview”: True,
“disable_notification”: False,
}
for attempt in range(1, retries + 1):
try:
r = requests.post(tg_url, json=payload, timeout=15)
r.raise_for_status()
log.info(“Telegram message sent.”)
return True
except requests.exceptions.HTTPError as e:
log.error(f”Telegram HTTP error (attempt {attempt}): {e.response.status_code}”)
except Exception as e:
log.error(f”Telegram error (attempt {attempt}): {type(e).**name**}”)
if attempt < retries:
time.sleep(5)
return False

def build_scenario1_message(price, offers, is_reminder=False):
tag = “🔔 <b>REMINDER</b>” if is_reminder else “🚨 <b>PRICE DROP ALERT</b>”
lines = [
tag, “”,
“🎮 <b>PS5 Slim 1024GB</b>”,
f”📉 Listed Price: <b>₹{price:,}</b>”,
f”🎯 Your Target: ₹{TARGET:,}”,
f”💰 Below target by: ₹{TARGET - price:,}”,
“”,
]
if offers:
lines.append(“💳 <b>Active Card Offers:</b>”)
for o in offers:
lines.append(f”  • {o[‘bank’]} {o[‘card_type’]}: ₹{o[‘discount’]:,} off”)
lines += [””, f”🏷️ Best Effective Price: <b>₹{price - offers[0][‘discount’]:,}</b>”]
lines += [
“”,
f”🔗 <a href='{PRODUCT_URL}'>Buy on Flipkart</a>”,
f”⏰ {datetime.now().strftime(’%d %b %Y, %I:%M %p’)}”,
]
return “\n”.join(lines)

def build_scenario2_message(price, offers, best_offer, effective_price, is_reminder=False):
tag = “🔔 <b>REMINDER</b>” if is_reminder else “💳 <b>CARD OFFER ALERT</b>”
lines = [
tag, “”,
“🎮 <b>PS5 Slim 1024GB</b>”,
f”🏷️ Listed Price: ₹{price:,}”,
“”,
“💳 <b>All Active Card Offers:</b>”,
]
for o in offers:
lines.append(f”  • {o[‘bank’]} {o[‘card_type’]}: ₹{o[‘discount’]:,} off → Effective ₹{price - o[‘discount’]:,}”)
lines += [
“”,
f”🏆 Best Deal: <b>{best_offer[‘bank’]} {best_offer[‘card_type’]}</b>”,
f”   ₹{best_offer[‘discount’]:,} off → Effective Price: <b>₹{effective_price:,}</b>”,
f”🎯 Your Target: ₹{TARGET:,}”,
f”💰 Under target by: ₹{TARGET - effective_price:,}”,
“”,
f”🔗 <a href='{PRODUCT_URL}'>Buy on Flipkart</a>”,
f”⏰ {datetime.now().strftime(’%d %b %Y, %I:%M %p’)}”,
]
return “\n”.join(lines)

# ─────────────────────────────────────────────

# ALERT LOGIC

# ─────────────────────────────────────────────

def should_alert(scenario_state, is_triggered):
count   = scenario_state[“count”]
last_ts = scenario_state[“last_alert_ts”]
now_ts  = datetime.now(timezone.utc).timestamp()

```
if not is_triggered:
    return None
if count == 0:
    return "first"
if count == 1 and last_ts:
    if (now_ts - last_ts) / 60 >= REMINDER_MINUTES:
        return "reminder"
return None
```

def update_scenario_state(s):
s[“count”] += 1
s[“last_alert_ts”] = datetime.now(timezone.utc).timestamp()

def reset_scenario(state, key):
if state[key][“count”] > 0:
log.info(f”{key} recovered — resetting state.”)
state[key] = {“count”: 0, “last_alert_ts”: None}
return True
return False

# ─────────────────────────────────────────────

# MAIN

# ─────────────────────────────────────────────

def main():
log.info(“Starting Flipkart price check…”)
validate_config()
state = load_state()

```
# Attempt 1: static HTML (1 credit)
html  = fetch_page(PRODUCT_URL, render=False)
soup  = BeautifulSoup(html, "lxml") if html else None
price = extract_price(soup) if soup else None

# Attempt 2: JS-rendered fallback (5 credits)
if price is None:
    log.info("Retrying with JS rendering...")
    html  = fetch_page(PRODUCT_URL, render=True)
    soup  = BeautifulSoup(html, "lxml") if html else None
    price = extract_price(soup) if soup else None

if price is None:
    log.warning("Price not found after both attempts. Skipping.")
    return

offers = extract_offers(soup)
log.info(f"Listed price: ₹{price:,}")
log.info(f"Offers found: {len(offers)}")
for o in offers:
    log.info(f"  {o['bank']} {o['card_type']}: ₹{o['discount']:,} off")

state_changed = False

# ── SCENARIO 1 ──
s1_triggered = price < TARGET
s1_action    = should_alert(state["scenario1"], s1_triggered)

if s1_action:
    msg = build_scenario1_message(price, offers, is_reminder=(s1_action == "reminder"))
    if send_telegram(msg):
        update_scenario_state(state["scenario1"])
        state_changed = True
        log.info(f"Scenario 1 alert sent ({s1_action}).")
elif s1_triggered:
    log.info("Scenario 1: max alerts already sent.")
else:
    log.info(f"Scenario 1: ₹{price:,} above target ₹{TARGET:,}.")
    if reset_scenario(state, "scenario1"):
        state_changed = True

# ── SCENARIO 2 ──
best_offer      = None
effective_price = None
s2_triggered    = False

if offers and price >= TARGET:
    best_offer      = offers[0]
    effective_price = price - best_offer["discount"]
    s2_triggered    = effective_price < TARGET
    log.info(f"Scenario 2: effective ₹{effective_price:,}")

s2_action = should_alert(state["scenario2"], s2_triggered)

if s2_action:
    msg = build_scenario2_message(
        price, offers, best_offer, effective_price,
        is_reminder=(s2_action == "reminder")
    )
    if send_telegram(msg):
        update_scenario_state(state["scenario2"])
        state_changed = True
        log.info(f"Scenario 2 alert sent ({s2_action}).")
elif s2_triggered:
    log.info("Scenario 2: max alerts already sent.")
else:
    if reset_scenario(state, "scenario2"):
        state_changed = True

if state_changed:
    save_state(state)

log.info("Check complete.")
```

if **name** == “**main**”:
main()
