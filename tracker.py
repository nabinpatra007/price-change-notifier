"""
Flipkart PS5 Price Tracker Bot
Monitors listed price + instant card offers and sends Telegram alerts.
"""

import os
import sys
import json
import re
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ─────────────────────────────────────────────
# CONFIG (all from GitHub Secrets / env vars)
# ─────────────────────────────────────────────
BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID        = os.environ["TELEGRAM_CHAT_ID"]
PRODUCT_URL    = os.environ["FLIPKART_URL"]
SCRAPER_API_KEY = os.environ["SCRAPER_API_KEY"]
TARGET         = 44900
STATE_FILE     = "state.json"
MAX_ALERTS     = 2
REMINDER_MINUTES = 30

# Price sanity bounds — guard against scraping glitches
PRICE_MIN = 5_000
PRICE_MAX = 2_00_000

SCRAPER_API_URL = "https://api.scraperapi.com"

# ─────────────────────────────────────────────
# STARTUP VALIDATION
# ─────────────────────────────────────────────
def validate_config():
    """Validate secrets and config at startup. Abort early if anything is wrong."""
    errors = []

    # Validate Telegram token format: digits:alphanumeric_string
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{35,}', BOT_TOKEN):
        errors.append("TELEGRAM_BOT_TOKEN format looks invalid.")

    # Validate Chat ID is numeric (can be negative for group chats)
    if not re.fullmatch(r'-?\d+', CHAT_ID.strip()):
        errors.append("TELEGRAM_CHAT_ID must be a numeric value.")

    # Validate Flipkart URL — must be HTTPS and from flipkart.com only
    try:
        parsed = urlparse(PRODUCT_URL)
        if parsed.scheme != "https":
            errors.append("FLIPKART_URL must use HTTPS.")
        if not (parsed.netloc == "www.flipkart.com" or parsed.netloc == "flipkart.com"):
            errors.append("FLIPKART_URL must be from flipkart.com only.")
    except Exception:
        errors.append("FLIPKART_URL is not a valid URL.")

    # Validate ScraperAPI key — just check it's not empty
    if not SCRAPER_API_KEY or len(SCRAPER_API_KEY.strip()) < 10:
        errors.append("SCRAPER_API_KEY looks invalid or too short.")
        for e in errors:
            log.error(f"Config error: {e}")
        sys.exit(1)

    log.info("Config validation passed.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# HEADERS — rotated to avoid Flipkart blocks
# ─────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
    "DNT": "1",
}


# ─────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────
def load_state():
    default = {
        "scenario1": {"count": 0, "last_alert_ts": None},
        "scenario2": {"count": 0, "last_alert_ts": None},
    }
    if Path(STATE_FILE).exists():
        try:
            with open(STATE_FILE) as f:
                loaded = json.load(f)
            # Validate structure — don't trust file blindly
            for key in ("scenario1", "scenario2"):
                if key not in loaded:
                    raise ValueError(f"Missing key: {key}")
                if "count" not in loaded[key] or "last_alert_ts" not in loaded[key]:
                    raise ValueError(f"Malformed scenario state for {key}")
                if not isinstance(loaded[key]["count"], int):
                    raise ValueError(f"count must be int for {key}")
            return loaded
        except Exception as e:
            log.warning(f"state.json invalid ({e}), resetting to default.")
    return default


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.info("State saved.")


# ─────────────────────────────────────────────
# SCRAPING
# ─────────────────────────────────────────────
def fetch_page(url):
    """Fetch Flipkart page via ScraperAPI — handles IP rotation automatically."""
    try:
        payload = {
            "api_key": SCRAPER_API_KEY,
            "url": url,
            "render": "false",      # No JS rendering needed — price is in HTML
            "country_code": "in",   # Use Indian IP for correct pricing
        }
        resp = requests.get(
            SCRAPER_API_URL,
            params=payload,
            timeout=60              # ScraperAPI needs more time than direct requests
        )
        if resp.status_code == 200:
            log.info("Page fetched successfully via ScraperAPI.")
            return resp.text
        else:
            log.error(f"ScraperAPI returned status {resp.status_code}")
            return None
    except Exception as e:
        log.error(f"Failed to fetch page: {type(e).__name__}")
        return None


def extract_price(soup):
    """Try multiple known Flipkart price CSS classes."""
    selectors = ["._30jeq3", ".Nx9bqj", "._16Jk6d", "._1vC4OE"]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            raw = el.get_text(strip=True).replace("₹", "").replace(",", "").strip()
            try:
                price = int(float(raw))
                # Sanity check — guard against scraping glitches
                if PRICE_MIN <= price <= PRICE_MAX:
                    return price
                else:
                    log.warning(f"Price {price} outside sanity bounds ({PRICE_MIN}–{PRICE_MAX}). Ignoring.")
            except ValueError:
                continue
    log.warning("Could not find price on page.")
    return None


def extract_offers(soup):
    """
    Extract all instant bank/card offers from Flipkart page.
    Returns list of dicts: [{bank, discount_amount, raw_text}, ...]
    Only flat instant discounts (not EMI, not percentage-only).
    """
    offers = []
    seen = set()

    # Multiple containers Flipkart uses for offers
    offer_containers = soup.select(
        "._2Tpdn3, .offer-wrap, ._3xFOBe, ._2AkmmA, .TVhoEJ, "
        "li._7eSDEz, ._1LKTO3, .offer-item, ._3HMbXn"
    )

    # Also grab any text blocks that mention bank offers
    all_text_blocks = soup.find_all(
        string=re.compile(
            r'(HDFC|SBI|ICICI|Axis|Kotak|RBL|IDFC|IndusInd|Yes Bank|Citi|HSBC|'
            r'American Express|Amex|BOB|Bank of Baroda)',
            re.IGNORECASE
        )
    )

    raw_texts = [el.get_text(" ", strip=True) for el in offer_containers]
    raw_texts += [str(t) for t in all_text_blocks]

    for text in raw_texts:
        if not text or text in seen:
            continue
        seen.add(text)

        # Skip EMI-only offers
        if re.search(r'\bEMI\b', text, re.IGNORECASE) and \
           not re.search(r'instant|cashback|off\b', text, re.IGNORECASE):
            continue

        # Match flat rupee discounts: "₹10,000 off", "Rs.5000 off", "10000 off"
        match = re.search(
            r'(?:₹|Rs\.?)\s*([\d,]+)\s*(?:off|instant|discount)',
            text, re.IGNORECASE
        )
        if not match:
            # Try reverse: "instant discount of ₹5000"
            match = re.search(
                r'instant.*?(?:₹|Rs\.?)\s*([\d,]+)',
                text, re.IGNORECASE
            )

        if match:
            amount_str = match.group(1).replace(",", "")
            try:
                amount = int(amount_str)
            except ValueError:
                continue

            # Only care about meaningful discounts (≥ 500)
            if amount < 500:
                continue

            # Detect bank name
            bank_match = re.search(
                r'(HDFC|SBI|ICICI|Axis|Kotak|RBL|IDFC|IndusInd|Yes Bank|'
                r'Citi|HSBC|Amex|American Express|BOB|Bank of Baroda)',
                text, re.IGNORECASE
            )
            bank = bank_match.group(1).upper() if bank_match else "Bank"

            # Detect card type
            card_type = "Card"
            if re.search(r'credit', text, re.IGNORECASE):
                card_type = "Credit Card"
            elif re.search(r'debit', text, re.IGNORECASE):
                card_type = "Debit Card"

            offers.append({
                "bank": bank,
                "card_type": card_type,
                "discount": amount,
                "raw": text[:120]
            })

    # Deduplicate by (bank, discount)
    seen_pairs = set()
    unique_offers = []
    for o in offers:
        key = (o["bank"], o["discount"])
        if key not in seen_pairs:
            seen_pairs.add(key)
            unique_offers.append(o)

    return sorted(unique_offers, key=lambda x: x["discount"], reverse=True)


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message):
    # SECURITY: Never log or f-string the full URL — token would appear in
    # GitHub Actions logs. Build URL silently and never print it.
    tg_url = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": False,  # Force notification even if chat is muted
    }
    try:
        r = requests.post(tg_url, json=payload, timeout=15)
        r.raise_for_status()
        log.info("Telegram message sent successfully.")
        return True
    except requests.exceptions.HTTPError as e:
        # Log status code only — never log the URL (contains token)
        log.error(f"Telegram HTTP error: status {e.response.status_code}")
        return False
    except Exception as e:
        # Log exception type only — not full repr which may include URL
        log.error(f"Telegram send failed: {type(e).__name__}")
        return False


def build_scenario1_message(price, offers, is_reminder=False):
    tag = "🔔 <b>REMINDER</b>" if is_reminder else "🚨 <b>PRICE DROP ALERT</b>"
    lines = [
        tag,
        "",
        "🎮 <b>PS5 Slim 1024GB</b>",
        f"📉 Listed Price: <b>₹{price:,}</b>",
        f"🎯 Your Target: ₹{TARGET:,}",
        f"💰 Below target by: ₹{TARGET - price:,}",
        "",
    ]
    if offers:
        lines.append("💳 <b>Active Card Offers:</b>")
        for o in offers:
            lines.append(f"  • {o['bank']} {o['card_type']}: ₹{o['discount']:,} off")
        best = offers[0]["discount"]
        effective = price - best
        lines.append("")
        lines.append(f"🏷️ Best Effective Price: <b>₹{effective:,}</b>")
    lines += [
        "",
        f"🔗 <a href='{PRODUCT_URL}'>Buy on Flipkart</a>",
        f"⏰ Checked at: {datetime.now().strftime('%d %b %Y, %I:%M %p')}",
    ]
    return "\n".join(lines)


def build_scenario2_message(price, offers, best_offer, effective_price, is_reminder=False):
    tag = "🔔 <b>REMINDER</b>" if is_reminder else "💳 <b>CARD OFFER ALERT</b>"
    lines = [
        tag,
        "",
        "🎮 <b>PS5 Slim 1024GB</b>",
        f"🏷️ Listed Price: ₹{price:,}",
        "",
        "💳 <b>All Active Card Offers:</b>",
    ]
    for o in offers:
        eff = price - o["discount"]
        lines.append(
            f"  • {o['bank']} {o['card_type']}: ₹{o['discount']:,} off "
            f"→ Effective ₹{eff:,}"
        )
    lines += [
        "",
        f"🏆 Best Deal: <b>{best_offer['bank']} {best_offer['card_type']}</b>",
        f"   ₹{best_offer['discount']:,} off → Effective Price: <b>₹{effective_price:,}</b>",
        f"🎯 Your Target: ₹{TARGET:,}",
        f"💰 Under target by: ₹{TARGET - effective_price:,}",
        "",
        f"🔗 <a href='{PRODUCT_URL}'>Buy on Flipkart</a>",
        f"⏰ Checked at: {datetime.now().strftime('%d %b %Y, %I:%M %p')}",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# ALERT LOGIC WITH STATE
# ─────────────────────────────────────────────
def should_alert(scenario_state, is_triggered):
    """
    Returns: "first", "reminder", or None
    - "first"    → first alert, trigger just fired
    - "reminder" → second alert, 30 mins after first
    - None       → no alert (already done, or not triggered)
    """
    count = scenario_state["count"]
    last_ts = scenario_state["last_alert_ts"]
    now_ts = datetime.now(timezone.utc).timestamp()

    if not is_triggered:
        return None
    if count == 0:
        return "first"
    if count == 1 and last_ts:
        mins_elapsed = (now_ts - last_ts) / 60
        if mins_elapsed >= REMINDER_MINUTES:
            return "reminder"
    return None  # Already sent both alerts


def update_scenario_state(scenario_state):
    scenario_state["count"] += 1
    scenario_state["last_alert_ts"] = datetime.now(timezone.utc).timestamp()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    log.info("Starting Flipkart price check...")
    validate_config()
    state = load_state()

    html = fetch_page(PRODUCT_URL)
    if not html:
        log.error("Could not fetch Flipkart page. Exiting.")
        return

    soup = BeautifulSoup(html, "html.parser")
    price = extract_price(soup)
    offers = extract_offers(soup)

    if price is None:
        log.warning("Price not found. Flipkart may be blocking. Skipping this run.")
        return

    log.info(f"Listed price: ₹{price:,}")
    log.info(f"Offers found: {len(offers)}")
    for o in offers:
        log.info(f"  {o['bank']} {o['card_type']}: ₹{o['discount']:,} off")

    state_changed = False

    # ── SCENARIO 1: Listed price dropped below target ──
    s1_triggered = price < TARGET
    s1_action = should_alert(state["scenario1"], s1_triggered)

    if s1_action:
        is_reminder = s1_action == "reminder"
        msg = build_scenario1_message(price, offers, is_reminder=is_reminder)
        if send_telegram(msg):
            update_scenario_state(state["scenario1"])
            state_changed = True
            log.info(f"Scenario 1 alert sent ({s1_action}).")
    elif s1_triggered:
        log.info("Scenario 1 triggered but max alerts already sent.")
    else:
        log.info(f"Scenario 1: listed price ₹{price:,} is above target ₹{TARGET:,}.")
        # Reset state if price goes back above target (so future drops re-trigger)
        if state["scenario1"]["count"] > 0:
            log.info("Price recovered above target — resetting Scenario 1 state.")
            state["scenario1"] = {"count": 0, "last_alert_ts": None}
            state_changed = True

    # ── SCENARIO 2: Effective price (after offers) dropped below target ──
    best_offer = None
    effective_price = None
    s2_triggered = False

    if offers and price >= TARGET:
        # Only evaluate offers if listed price hasn't already triggered Scenario 1
        best_offer = offers[0]
        effective_price = price - best_offer["discount"]
        s2_triggered = effective_price < TARGET
        log.info(f"Scenario 2: effective price ₹{effective_price:,} (₹{price:,} - ₹{best_offer['discount']:,})")

    s2_action = should_alert(state["scenario2"], s2_triggered)

    if s2_action:
        is_reminder = s2_action == "reminder"
        msg = build_scenario2_message(
            price, offers, best_offer, effective_price, is_reminder=is_reminder
        )
        if send_telegram(msg):
            update_scenario_state(state["scenario2"])
            state_changed = True
            log.info(f"Scenario 2 alert sent ({s2_action}).")
    elif s2_triggered:
        log.info("Scenario 2 triggered but max alerts already sent.")
    else:
        # Reset if offers disappear or effective price goes back above target
        if state["scenario2"]["count"] > 0:
            log.info("Effective price recovered — resetting Scenario 2 state.")
            state["scenario2"] = {"count": 0, "last_alert_ts": None}
            state_changed = True

    if state_changed:
        save_state(state)

    log.info("Check complete.")


if __name__ == "__main__":
    main()
