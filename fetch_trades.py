#!/usr/bin/env python3
"""Fetch PoE2 trade history + current listings, generate dashboard data."""

import argparse
import csv
import hashlib
import json
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
import os

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
TRADES_FILE = DATA_DIR / "trades.json"
DASHBOARD_FILE = DATA_DIR / "dashboard.json"
INVESTORS_FILE = DATA_DIR / "investors.json"

HAIRCUT = 0.85
PERF_FEE_PCT = 0.25

HISTORY_URL = "https://www.pathofexile.com/api/trade2/history/{league}"
SEARCH_URL = "https://www.pathofexile.com/api/trade2/search/poe2/{league}"
FETCH_URL = "https://www.pathofexile.com/api/trade2/fetch/{ids}?query={query_id}&realm=poe2"
RATES_URL = "https://poe2scout.com/api/currencyExchange/SnapshotPairs?league={league}"


def get_config():
    poesessid = os.getenv("POESESSID")
    if not poesessid:
        print("Error: POESESSID not set in .env file")
        sys.exit(1)
    return {
        "poesessid": poesessid,
        "league": os.getenv("LEAGUE", "Fate of the Vaal"),
        "account": os.getenv("ACCOUNT", "Stakenborg#4677"),
        "sheet_id": os.getenv("SHEET_ID", ""),
        "google_credentials": os.getenv("GOOGLE_CREDENTIALS", "credentials.json"),
        "sales_tab": os.getenv("SALES_TAB", "Sales"),
    }


def make_session(poesessid):
    s = requests.Session()
    s.cookies.set("POESESSID", poesessid)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    })
    return s


def api_request(session, method, url, max_retries=3, **kwargs):
    """Make an API request with rate-limit retry and backoff."""
    for attempt in range(max_retries):
        resp = session.request(method, url, **kwargs)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            if retry_after > 30:
                print(f"  Rate limited for {retry_after}s — too long, skipping")
                resp.raise_for_status()
            print(f"  Rate limited — waiting {retry_after}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


# --- Exchange Rates ---

def fetch_exchange_rates(league):
    """Fetch currency exchange rates from poe2scout, returns rates relative to divine."""
    url = RATES_URL.format(league=quote(league))
    headers = {"User-Agent": "poe2-investments (local fund tracker)"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        pairs = resp.json()
    except Exception as e:
        print(f"Warning: Could not fetch exchange rates: {e}")
        return {}

    rates = {"divine": 1.0}
    for pair in pairs:
        c1_id = pair["CurrencyOne"]["apiId"]
        c2_id = pair["CurrencyTwo"]["apiId"]
        c1_price = float(pair["CurrencyOneData"]["RelativePrice"])
        c2_price = float(pair["CurrencyTwoData"]["RelativePrice"])

        if c1_id == "divine" and c2_id not in rates:
            rates[c2_id] = c2_price / c1_price if c1_price else 0
        elif c2_id == "divine" and c1_id not in rates:
            rates[c1_id] = c1_price / c2_price if c2_price else 0

    return rates


def to_divine(amount, currency, rates):
    """Convert an amount in any currency to divine equivalent."""
    if currency == "divine":
        return amount
    rate = rates.get(currency)
    if rate:
        return round(amount * rate, 2)
    return None


# --- Trade History ---

def fetch_trades(session, league):
    url = HISTORY_URL.format(league=quote(league))
    resp = api_request(session, "GET", url)
    return resp.json().get("result", [])


def _get_extended(item):
    ext = item.get("extended", {})
    if not isinstance(ext, dict):
        return {}, {}
    return ext.get("mods", {}), ext.get("hashes", {})


def parse_trade(trade, rates=None):
    item = trade.get("item", {})
    price = trade.get("price", {})
    name = item.get("name") or item.get("typeLine") or "Unknown"
    base_type = item.get("baseType", "")
    currency = price.get("currency", "")
    amount = price.get("amount", 0)

    if rates:
        div_equivalent = to_divine(amount, currency, rates)
        if div_equivalent is None:
            div_equivalent = ""
    else:
        div_equivalent = amount if currency == "divine" else ""

    return {
        "timestamp": trade.get("time", ""),
        "item_name": name,
        "base_type": base_type,
        "rarity": item.get("rarity", ""),
        "sale_price": amount,
        "currency": currency,
        "div_equivalent": div_equivalent,
        "item_id": trade.get("item_id", ""),
        "icon": item.get("icon", ""),
        "ilvl": item.get("ilvl", 0),
        "corrupted": item.get("corrupted", False),
        "double_corrupted": item.get("doubleCorrupted", False),
        "sanctified": item.get("sanctified", False),
        "implicit_mods": item.get("implicitMods", []),
        "explicit_mods": item.get("explicitMods", []),
        "enchant_mods": item.get("enchantMods", []),
        "desecrated_mods": item.get("desecratedMods", []),
        "fractured_mods": item.get("fracturedMods", []),
        "flavour_text": item.get("flavourText", []),
        "frame_type": item.get("frameType", 0),
        "type_line": item.get("typeLine", ""),
        "properties": item.get("properties", []),
        "sockets": item.get("sockets", []),
        "socketed_items": item.get("socketedItems", []),
        "rune_mods": item.get("runeMods", []),
        "granted_skills": item.get("grantedSkills", []),
        "extended_mods": _get_extended(item)[0],
        "extended_hashes": _get_extended(item)[1],
    }


def load_seen_trades():
    if TRADES_FILE.exists():
        with open(TRADES_FILE) as f:
            return json.load(f)
    return []


def save_trades(trades):
    DATA_DIR.mkdir(exist_ok=True)
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def find_new_trades(fetched, seen):
    seen_ids = {t["item_id"] for t in seen}
    new = []
    for trade in fetched:
        item_id = trade.get("item_id", "")
        if item_id and item_id not in seen_ids:
            new.append(trade)
    return new


# --- Current Listings ---

def fetch_listings(session, league, account):
    url = SEARCH_URL.format(league=quote(league))
    payload = {
        "query": {
            "status": {"option": "securable"},
            "stats": [{"type": "and", "filters": [], "disabled": False}],
            "filters": {
                "trade_filters": {
                    "filters": {"account": {"input": account}},
                    "disabled": False,
                }
            },
        },
        "sort": {"price": "asc"},
    }

    resp = api_request(session, "POST", url, json=payload)
    search_data = resp.json()

    query_id = search_data.get("id", "")
    item_ids = search_data.get("result", [])

    if not item_ids:
        return []

    # Fetch endpoint accepts max 10 IDs at a time
    listings = []
    for i in range(0, len(item_ids), 10):
        batch = item_ids[i:i + 10]
        ids_str = ",".join(batch)
        fetch_url = FETCH_URL.format(ids=ids_str, query_id=query_id)
        resp = api_request(session, "GET", fetch_url)
        listings.extend(resp.json().get("result", []))
        if i + 10 < len(item_ids):
            time.sleep(2)

    return listings


def parse_listing(entry, rates=None):
    item = entry.get("item", {})
    listing = entry.get("listing", {})
    price = listing.get("price", {})
    name = item.get("name") or item.get("typeLine") or "Unknown"
    base_type = item.get("baseType", "")
    currency = price.get("currency", "")
    amount = price.get("amount", 0)

    if rates:
        div_equivalent = to_divine(amount, currency, rates)
        if div_equivalent is None:
            div_equivalent = ""
    else:
        div_equivalent = amount if currency == "divine" else ""

    return {
        "item_id": entry.get("id", ""),
        "item_name": name,
        "base_type": base_type,
        "rarity": item.get("rarity", ""),
        "listed_price": amount,
        "currency": currency,
        "div_equivalent": div_equivalent,
        "indexed": listing.get("indexed", ""),
        "stash": listing.get("stash", {}).get("name", ""),
        "icon": item.get("icon", ""),
        "ilvl": item.get("ilvl", 0),
        "corrupted": item.get("corrupted", False),
        "double_corrupted": item.get("doubleCorrupted", False),
        "sanctified": item.get("sanctified", False),
        "implicit_mods": item.get("implicitMods", []),
        "explicit_mods": item.get("explicitMods", []),
        "enchant_mods": item.get("enchantMods", []),
        "desecrated_mods": item.get("desecratedMods", []),
        "fractured_mods": item.get("fracturedMods", []),
        "flavour_text": item.get("flavourText", []),
        "frame_type": item.get("frameType", 0),
        "type_line": item.get("typeLine", ""),
        "properties": item.get("properties", []),
        "sockets": item.get("sockets", []),
        "socketed_items": item.get("socketedItems", []),
        "rune_mods": item.get("runeMods", []),
        "granted_skills": item.get("grantedSkills", []),
        "extended_mods": _get_extended(item)[0],
        "extended_hashes": _get_extended(item)[1],
    }


# --- Dashboard ---

def load_dashboard():
    if DASHBOARD_FILE.exists():
        with open(DASHBOARD_FILE) as f:
            return json.load(f)
    return {}


def save_dashboard(data):
    DATA_DIR.mkdir(exist_ok=True)
    with open(DASHBOARD_FILE, "w") as f:
        json.dump(data, f, indent=2)


def build_dashboard(trades, listings, currencies, rates, inv_data=None):
    parsed_listings = [parse_listing(l, rates) for l in listings]
    listed_value = sum(
        l["div_equivalent"] for l in parsed_listings
        if isinstance(l["div_equivalent"], (int, float))
    )

    raw_divines = currencies_to_divine(currencies, rates)
    adjusted_nav = calc_nav(currencies, rates, listed_value)

    dashboard = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "currencies": currencies,
        "raw_divines": raw_divines,
        "listed_value": listed_value,
        "total_nav": adjusted_nav,
        "raw_nav": raw_divines + listed_value,
        "haircut": HAIRCUT,
        "exchange_rates": rates,
        "listings": parsed_listings,
        "recent_sales": trades[:50],  # Last 50 sales for the dashboard
    }

    # Include investor data if available
    if inv_data and inv_data.get("investors"):
        inv_data = recalc_investors(inv_data, adjusted_nav)
        save_investors(inv_data)
        pub = investors_to_dashboard(inv_data)
        dashboard["fund"] = pub["fund"]
        dashboard["investors"] = pub["investors"]

    return dashboard


# --- CSV Export ---

def export_csv(trades, path):
    rows = [parse_trade(t) for t in trades]
    if not rows:
        return
    fieldnames = ["timestamp", "item_name", "base_type", "rarity", "sale_price", "currency", "div_equivalent"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# --- Google Sheets ---

def push_to_sheets(trades, config):
    import gspread

    creds_path = Path(config["google_credentials"])
    if not creds_path.exists():
        print(f"Error: Google credentials file not found at {creds_path}")
        print("See .env.example for setup instructions")
        sys.exit(1)

    if not config["sheet_id"]:
        print("Error: SHEET_ID not set in .env file")
        sys.exit(1)

    gc = gspread.service_account(filename=str(creds_path))
    sheet = gc.open_by_key(config["sheet_id"])
    worksheet = sheet.worksheet(config["sales_tab"])

    rows = []
    for trade in trades:
        parsed = parse_trade(trade)
        rows.append([
            parsed["timestamp"],
            parsed["item_name"],
            parsed["base_type"],
            parsed["rarity"],
            parsed["sale_price"],
            parsed["currency"],
            parsed["div_equivalent"],
        ])

    if rows:
        worksheet.insert_rows(rows, row=2)
        print(f"Pushed {len(rows)} rows to Google Sheets")


# --- Investor Management ---

def load_investors():
    """Load private investors file (has plaintext codes). Decrypts from .enc if needed."""
    if not INVESTORS_FILE.exists():
        decrypt_investors()
    if INVESTORS_FILE.exists():
        with open(INVESTORS_FILE) as f:
            return json.load(f)
    return {"fund": _default_fund_config(), "investors": []}


def save_investors(data):
    DATA_DIR.mkdir(exist_ok=True)
    with open(INVESTORS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _default_fund_config():
    return {
        "currencies": {"divine": 0},
        "total_units": 0,
        "unit_price": 1.0,
        "hwm": 1.0,
        "haircut": HAIRCUT,
        "perf_fee_pct": PERF_FEE_PCT,
        "total_deposited": 0,
        "total_profit": 0,
        "discord_webhook": "",
    }


def migrate_fund_data(inv_data, prev_dashboard):
    """Migrate fund data to multi-currency format if needed."""
    fund = inv_data["fund"]
    if "currencies" not in fund:
        fund["currencies"] = {"divine": prev_dashboard.get("raw_divines", 0)}
    return inv_data


def currencies_to_divine(currencies, rates):
    """Sum all currency values in divine terms."""
    return sum(amt * rates.get(cur, 0) for cur, amt in currencies.items() if rates.get(cur) is not None)


def hash_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


def calc_nav(currencies, rates, listed_value):
    """NAV = liquid (all currencies in divine terms) + listed * haircut."""
    liquid = currencies_to_divine(currencies, rates)
    return liquid + listed_value * HAIRCUT


def recalc_investors(inv_data, nav):
    """Recalculate all investor values, shares, and profits from current NAV."""
    fund = inv_data["fund"]
    total_units = fund["total_units"]
    unit_price = nav / total_units if total_units > 0 else 1.0

    # --- Crystallize performance fee if price > HWM ---
    hwm = fund.get("hwm", 1.0)
    if unit_price > hwm and total_units > 0 and len(inv_data["investors"]) > 0:
        gain_per_unit = unit_price - hwm
        # Fee applies to all non-manager units
        manager = inv_data["investors"][0]
        non_manager_units = total_units - manager["units"]
        if non_manager_units > 0:
            fee_value = gain_per_unit * non_manager_units * PERF_FEE_PCT
            fee_units = fee_value / unit_price
            manager["units"] += fee_units
            fund["total_units"] += fee_units
            total_units = fund["total_units"]
            # Recalc unit price after minting (NAV unchanged, more units)
            unit_price = nav / total_units
            fund["hwm"] = round(nav / total_units, 6)  # HWM = new unit price post-mint
            print(f"  Performance fee crystallized: {fee_value:,.2f} div ({fee_units:,.4f} units minted to {manager['name']})")

    fund["unit_price"] = round(unit_price, 6)

    total_value = 0
    total_profit = 0
    total_deposited = 0

    for investor in inv_data["investors"]:
        value = round(investor["units"] * unit_price, 2)
        profit = round(value - investor["deposited"], 2)
        share = investor["units"] / total_units if total_units > 0 else 0

        pct_change = ((value - investor["deposited"]) / investor["deposited"] * 100) if investor["deposited"] > 0 else None

        investor["value"] = value
        investor["profit"] = profit
        investor["share"] = round(share, 6)
        investor["pct_change"] = round(pct_change, 1) if pct_change is not None else None

        total_value += value
        total_profit += profit
        total_deposited += investor["deposited"]

    fund["total_deposited"] = round(total_deposited, 2)
    fund["total_profit"] = round(total_profit, 2)

    return inv_data


def find_investor(inv_data, name):
    for inv in inv_data["investors"]:
        if inv["name"].lower() == name.lower():
            return inv
    return None


def create_investor(inv_data, name):
    """Create a new investor with no position."""
    if find_investor(inv_data, name):
        print(f"Investor '{name}' already exists.")
        return inv_data

    confirm = input(f"Create new investor '{name}'? [y/N] ")
    if confirm.lower() != "y":
        print("Cancelled.")
        return None

    code = secrets.token_urlsafe(16)
    investor = {
        "name": name,
        "code": code,
        "hash": hash_code(code),
        "units": 0,
        "deposited": 0,
        "value": 0,
        "share": 0,
        "profit": 0,
        "pending": None,
        "history": [],
    }
    inv_data["investors"].append(investor)
    print(f"Created investor: {name}")
    print(f"Invite code: {code}")
    print(f"Share this code with them — it's their key to the personalized view.")
    return inv_data


def create_pending(inv_data, name, amount, currency, nav, rates, req_type):
    """Create a pending deposit or withdrawal request locked at current unit price."""
    fund = inv_data["fund"]
    total_units = fund["total_units"]
    unit_price = nav / total_units if total_units > 0 else 1.0

    investor = find_investor(inv_data, name)
    if not investor:
        print(f"Error: Investor '{name}' not found.")
        return None

    if investor["pending"]:
        print(f"Error: {name} already has a pending {investor['pending']['type']} request.")
        return None

    # Convert to divine equivalent for unit pricing
    div_equivalent = to_divine(amount, currency, rates)
    if div_equivalent is None:
        print(f"Error: No exchange rate for {currency}.")
        return None

    if req_type == "withdraw":
        current_value = investor["units"] * unit_price
        if div_equivalent > current_value:
            print(f"Error: Requested {div_equivalent:,.2f} div equivalent but position is only worth {current_value:,.2f} div.")
            return None

    investor["pending"] = {
        "type": req_type,
        "amount": round(div_equivalent, 2),
        "original_amount": amount,
        "currency": currency,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "locked_price": round(unit_price, 6),
    }

    label = "Deposit" if req_type == "deposit" else "Withdrawal"
    print(f"\n{label} request created for {investor['name']}:")
    print(f"  Amount: {amount:,.2f} {currency} ({div_equivalent:,.2f} div)")
    print(f"  Locked unit price: {unit_price:,.4f}")

    return inv_data


def process_fulfill(inv_data, name):
    """Fulfill a pending request (deposit or withdrawal)."""
    fund = inv_data["fund"]

    investor = find_investor(inv_data, name)
    if not investor:
        print(f"Error: Investor '{name}' not found.")
        return None

    pending = investor.get("pending")
    if not pending:
        print(f"Error: No pending request for {name}.")
        return None

    amount = pending["amount"]  # divine equivalent
    locked_price = pending["locked_price"]
    req_type = pending["type"]
    currency = pending.get("currency", "divine")
    original_amount = pending.get("original_amount", amount)

    # Update fund currency balance
    if "currencies" not in fund:
        fund["currencies"] = {"divine": 0}

    if req_type == "deposit":
        units_issued = amount / locked_price
        investor["units"] += units_issued
        investor["deposited"] += amount
        fund["total_units"] += units_issued
        fund["currencies"][currency] = fund["currencies"].get(currency, 0) + original_amount

        investor["pending"] = None
        history_entry = {
            "type": "deposit",
            "amount": amount,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "unit_price": locked_price,
        }
        if currency != "divine":
            history_entry["currency"] = currency
            history_entry["original_amount"] = original_amount
        investor["history"].append(history_entry)

        print(f"\nFulfilling deposit for {investor['name']}:")
        print(f"  Amount: {original_amount:,.2f} {currency} ({amount:,.2f} div)")
        print(f"  Units issued: {units_issued:,.4f}")
        print(f"  Total units (fund): {fund['total_units']:,.4f}")

    elif req_type == "withdraw":
        units_burned = amount / locked_price
        investor["units"] -= units_burned
        pct_withdrawn = units_burned / (investor["units"] + units_burned) if (investor["units"] + units_burned) > 0 else 1.0
        investor["deposited"] = round(investor["deposited"] * (1 - pct_withdrawn), 2)
        fund["total_units"] -= units_burned
        fund["currencies"][currency] = fund["currencies"].get(currency, 0) - original_amount

        investor["pending"] = None
        history_entry = {
            "type": "withdraw",
            "amount": amount,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "unit_price": locked_price,
        }
        if currency != "divine":
            history_entry["currency"] = currency
            history_entry["original_amount"] = original_amount
        investor["history"].append(history_entry)

        print(f"\nFulfilling withdrawal for {investor['name']}:")
        print(f"  Amount: {original_amount:,.2f} {currency} ({amount:,.2f} div)")
        print(f"  Units burned: {units_burned:,.4f}")
        print(f"  Remaining units (fund): {fund['total_units']:,.4f}")

    return inv_data


def generate_invite_code(inv_data, name):
    """Generate a new invite code for an investor."""
    investor = find_investor(inv_data, name)
    if not investor:
        print(f"Error: Investor '{name}' not found.")
        return None

    code = secrets.token_urlsafe(16)
    investor["code"] = code
    investor["hash"] = hash_code(code)

    print(f"\nNew invite code for {investor['name']}: {code}")
    print(f"Hash: {investor['hash']}")
    print("Share the code (not the hash) with them.")

    return inv_data


def investors_to_dashboard(inv_data):
    """Convert private investor data to public dashboard format (no plaintext codes)."""
    fund = {k: v for k, v in inv_data["fund"].items()}
    investors = []
    for inv in inv_data["investors"]:
        investors.append({
            "name": inv["name"],
            "hash": inv["hash"],
            "units": inv["units"],
            "deposited": inv["deposited"],
            "value": inv.get("value", 0),
            "share": inv.get("share", 0),
            "profit": inv.get("profit", 0),
            "pct_change": inv.get("pct_change", 0),
            "pending": inv.get("pending"),
            "history": inv.get("history", []),
        })
    return {"fund": fund, "investors": investors}


# --- CLI ---

def print_summary(new_trades, listings_summary):
    parsed = [parse_trade(t) for t in new_trades]
    total_div = sum(p["div_equivalent"] for p in parsed if isinstance(p["div_equivalent"], (int, float)))
    non_div = [p for p in parsed if p["div_equivalent"] == ""]

    print(f"\n{len(new_trades)} new trade(s)")
    if total_div:
        print(f"Revenue: {total_div:,.0f} divine")
    if non_div:
        print(f"  ({len(non_div)} trade(s) in non-divine currency — convert manually)")

    print("\nNew trades:")
    for p in parsed:
        price_str = f"{p['sale_price']} {p['currency']}"
        print(f"  {p['item_name']} ({p['base_type']}) — {price_str}")

    if listings_summary:
        print(f"\nCurrent listings: {listings_summary['count']} items, ~{listings_summary['value']:,.0f} divine listed value")


def encrypt_investors():
    """Encrypt investors.json to investors.json.enc using INVESTORS_KEY."""
    key = os.getenv("INVESTORS_KEY")
    if not key:
        print("Warning: INVESTORS_KEY not set, skipping encryption.")
        return False
    try:
        subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
             "-in", str(INVESTORS_FILE),
             "-out", str(INVESTORS_FILE) + ".enc",
             "-pass", f"pass:{key}"],
            check=True, capture_output=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"Encryption failed: {e.stderr.decode().strip() if e.stderr else e}")
        return False


def decrypt_investors():
    """Decrypt investors.json.enc to investors.json using INVESTORS_KEY."""
    enc_file = Path(str(INVESTORS_FILE) + ".enc")
    if not enc_file.exists():
        return False
    key = os.getenv("INVESTORS_KEY")
    if not key:
        return False
    try:
        subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-d",
             "-in", str(enc_file),
             "-out", str(INVESTORS_FILE),
             "-pass", f"pass:{key}"],
            check=True, capture_output=True,
        )
        print("Decrypted investors.json from encrypted store.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Decryption failed: {e.stderr.decode().strip() if e.stderr else e}")
        return False


def git_push():
    """Encrypt investors.json, commit and push dashboard + encrypted investors."""
    repo_root = Path(__file__).parent
    try:
        # Encrypt investors.json before committing
        encrypt_investors()

        subprocess.run(
            ["git", "add", "-f", "data/dashboard.json", "data/investors.json.enc"],
            cwd=repo_root, check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "status", "--porcelain", "data/dashboard.json", "data/investors.json.enc"],
            cwd=repo_root, check=True, capture_output=True, text=True,
        )
        if not result.stdout.strip():
            print("No data changes to push.")
            return
        subprocess.run(
            ["git", "commit", "-m", "Update fund data"],
            cwd=repo_root, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=repo_root, check=True, capture_output=True,
        )
        print("Pushed fund data to remote.")
    except subprocess.CalledProcessError as e:
        print(f"Git push failed: {e.stderr.decode().strip() if e.stderr else e}")


def process_batch(payload_str, config, prev_dashboard, inv_data):
    """Process a batch payload from the manager console / GitHub Actions."""
    payload = json.loads(payload_str)
    inv_data = migrate_fund_data(inv_data, prev_dashboard)
    currencies = inv_data["fund"]["currencies"]
    rates = prev_dashboard.get("exchange_rates", {"divine": 1.0})

    should_fetch = payload.get("fetch", False)
    should_fulfill = payload.get("fulfill", False)
    currency_overrides = payload.get("currencies", {})
    operations = payload.get("operations", [])

    seen = load_seen_trades()
    new_trades = []
    listings = []

    # Step 1: Fetch trades + listings if requested
    if should_fetch:
        session = make_session(config["poesessid"])

        print("Fetching exchange rates...")
        fetched_rates = fetch_exchange_rates(config["league"])
        if fetched_rates:
            rates = fetched_rates

        fetched = []
        print(f"Fetching trade history for {config['league']}...")
        try:
            fetched = fetch_trades(session, config["league"])
            print(f"Fetched {len(fetched)} trades from API")
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"  Trade history rate limited — skipping")
            else:
                raise

        new_trades = find_new_trades(fetched, seen) if fetched else []

        print("Fetching current listings...")
        listings = fetch_listings(session, config["league"], config["account"])
        print(f"Found {len(listings)} active listings")

        # Add trade revenue per-currency
        if new_trades:
            new_parsed_tmp = [parse_trade(t, rates) for t in new_trades]
            for t in new_parsed_tmp:
                cur = t.get("currency", "divine")
                amt = t.get("sale_price", 0)
                if cur and amt > 0:
                    currencies[cur] = currencies.get(cur, 0) + amt
                    print(f"  +{amt:,.0f} {cur} from trade")

    # Step 2: Apply currency overrides (only changed fields)
    for cur, amt in currency_overrides.items():
        currencies[cur] = amt
        print(f"  Currency override: {cur} = {amt:,.0f}")

    # Step 3: Create pending for each operation
    listed_value = prev_dashboard.get("listed_value", 0)
    if should_fetch and listings:
        parsed_listings = [parse_listing(l, rates) for l in listings]
        listed_value = sum(l["div_equivalent"] for l in parsed_listings if isinstance(l["div_equivalent"], (int, float)))

    nav = calc_nav(currencies, rates, listed_value)

    for op in operations:
        action = op["action"]
        investor_name = op["investor"]
        amount = op["amount"]
        currency = op.get("currency", "divine")
        result = create_pending(inv_data, investor_name, amount, currency, nav, rates, action)
        if result:
            inv_data = result

    # Step 4: Fulfill all pending if requested
    if should_fulfill:
        fulfilled_any = False
        for inv in list(inv_data["investors"]):
            if inv.get("pending"):
                result = process_fulfill(inv_data, inv["name"])
                if result:
                    inv_data = result
                    fulfilled_any = True
        if fulfilled_any:
            inv_data = recalc_investors(inv_data, nav)
        else:
            print("No pending requests to fulfill.")

    # Recalculate NAV after all mutations
    nav = calc_nav(currencies, rates, listed_value)

    # Step 5: Save everything
    inv_data["fund"]["currencies"] = currencies
    save_investors(inv_data)

    all_trades = (([parse_trade(t, rates) for t in new_trades] + seen) if new_trades else seen)
    if new_trades:
        save_trades(all_trades)

    if should_fetch and listings:
        dashboard = build_dashboard(all_trades, listings, currencies, rates, inv_data)
    else:
        raw_divines = currencies_to_divine(currencies, rates)
        prev_listings = prev_dashboard.get("listings", [])
        dashboard = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "currencies": currencies,
            "raw_divines": raw_divines,
            "listed_value": listed_value,
            "total_nav": nav,
            "raw_nav": raw_divines + listed_value,
            "haircut": HAIRCUT,
            "exchange_rates": rates,
            "listings": prev_listings,
            "recent_sales": all_trades[:50],
        }
        if inv_data.get("investors"):
            inv_data = recalc_investors(inv_data, nav)
            save_investors(inv_data)
            pub = investors_to_dashboard(inv_data)
            dashboard["fund"] = pub["fund"]
            dashboard["investors"] = pub["investors"]

    save_dashboard(dashboard)
    print(f"Dashboard data saved to {DASHBOARD_FILE}")
    return dashboard


def main():
    parser = argparse.ArgumentParser(description="Fetch PoE2 trade history + manage fund investors")
    parser.add_argument("--sheets", action="store_true", help="Push new trades to Google Sheets")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and display without saving")
    parser.add_argument("--divines", type=float, help="Update raw divine count in the fund")
    parser.add_argument("--push", action="store_true", help="Git commit + push dashboard data after update")

    # Investor management
    parser.add_argument("--add-investor", type=str, metavar="NAME", help="Add a new investor (combine with --deposit)")
    parser.add_argument("--deposit", nargs="+", metavar="ARG", help="Process deposit: --deposit NAME AMOUNT or --add-investor NAME --deposit AMOUNT")
    parser.add_argument("--withdraw", nargs=2, metavar=("NAME", "AMOUNT"), help="Create withdrawal request")
    parser.add_argument("--fulfill", action="store_true", help="Fulfill all pending requests")
    parser.add_argument("--gen-code", type=str, metavar="NAME", help="Generate new invite code for investor")
    parser.add_argument("--set-webhook", type=str, metavar="URL", help="Set Discord webhook URL for investor requests")
    parser.add_argument("--fetch", action="store_true", help="Fetch trades and listings from PoE2 API")
    parser.add_argument("--batch", type=str, metavar="PAYLOAD", help="Process batch JSON payload (from manager console)")

    args = parser.parse_args()

    config = get_config()

    # Load previous dashboard for persisted values
    prev_dashboard = load_dashboard()

    # Load investor data
    inv_data = load_investors()

    # Batch mode: short-circuit
    if args.batch:
        process_batch(args.batch, config, prev_dashboard, inv_data)
        if args.push:
            git_push()
        return

    # Migrate to multi-currency format
    inv_data = migrate_fund_data(inv_data, prev_dashboard)
    currencies = inv_data["fund"]["currencies"]

    # --divines flag sets divine currency (backward compat)
    if args.divines is not None:
        currencies["divine"] = args.divines

    # Default rates for no-fetch path
    rates = prev_dashboard.get("exchange_rates", {"divine": 1.0})

    # --- Handle investor-only operations ---

    is_investor_op = any([args.add_investor, args.deposit, args.withdraw, args.fulfill, args.gen_code, args.set_webhook])
    should_fetch = args.fetch

    if args.set_webhook:
        inv_data["fund"]["discord_webhook"] = args.set_webhook
        save_investors(inv_data)
        print(f"Discord webhook set.")
        if not is_investor_op or args.set_webhook == args.set_webhook:  # only op
            pass  # continue to potentially do other ops

    # Calculate current NAV from stored data if skipping fetch
    listed_value = prev_dashboard.get("listed_value", 0)
    nav = calc_nav(currencies, rates, listed_value)

    if args.gen_code:
        result = generate_invite_code(inv_data, args.gen_code)
        if result:
            save_investors(result)
        if not args.deposit and not args.withdraw and not args.fulfill and not args.add_investor:
            return

    if args.add_investor:
        result = create_investor(inv_data, args.add_investor)
        if not result:
            return
        inv_data = result
        save_investors(inv_data)

    if args.add_investor and args.deposit:
        # --add-investor NAME --deposit AMOUNT
        amount = float(args.deposit[0])
        result = create_pending(inv_data, args.add_investor, amount, "divine", nav, rates, "deposit")
        if result:
            inv_data = result
            save_investors(inv_data)
    elif args.deposit:
        # --deposit NAME AMOUNT
        if len(args.deposit) != 2:
            print("Usage: --deposit NAME AMOUNT")
            sys.exit(1)
        name, amount = args.deposit[0], float(args.deposit[1])
        result = create_pending(inv_data, name, amount, "divine", nav, rates, "deposit")
        if result:
            inv_data = result
            save_investors(inv_data)

    if args.withdraw:
        name, amount = args.withdraw[0], float(args.withdraw[1])
        result = create_pending(inv_data, name, amount, "divine", nav, rates, "withdraw")
        if result:
            inv_data = result
            save_investors(inv_data)

    if args.fulfill:
        fulfilled_any = False
        for inv in list(inv_data["investors"]):
            if inv.get("pending"):
                result = process_fulfill(inv_data, inv["name"])
                if result:
                    inv_data = result
                    fulfilled_any = True
        if fulfilled_any:
            inv_data = recalc_investors(inv_data, nav)
            save_investors(inv_data)
        else:
            print("No pending requests to fulfill.")

    # Unless --fetch, just rebuild dashboard from stored data and exit
    if not should_fetch:
        if not args.dry_run:
            all_trades = load_seen_trades()
            listings = prev_dashboard.get("listings", [])
            raw_divines = currencies_to_divine(currencies, rates)
            # Re-use existing parsed listings directly
            dashboard = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "currencies": currencies,
                "raw_divines": raw_divines,
                "listed_value": listed_value,
                "total_nav": nav,
                "raw_nav": raw_divines + listed_value,
                "haircut": HAIRCUT,
                "exchange_rates": rates,
                "listings": listings,
                "recent_sales": all_trades[:50],
            }
            if inv_data.get("investors"):
                inv_data = recalc_investors(inv_data, nav)
                save_investors(inv_data)
                pub = investors_to_dashboard(inv_data)
                dashboard["fund"] = pub["fund"]
                dashboard["investors"] = pub["investors"]
            save_dashboard(dashboard)
            print(f"Dashboard data saved to {DASHBOARD_FILE}")
            if args.push:
                git_push()
        return

    session = make_session(config["poesessid"])

    # Fetch exchange rates (overrides default rates)
    print("Fetching exchange rates...")
    fetched_rates = fetch_exchange_rates(config["league"])
    if fetched_rates:
        rates = fetched_rates
    if rates:
        non_divine = {k: v for k, v in rates.items() if k != "divine"}
        if non_divine:
            print("  Rates (per 1 divine):")
            for currency, rate in sorted(non_divine.items()):
                if rate > 0:
                    print(f"    {1/rate:,.2f} {currency}" if rate < 1 else f"    {currency}: {rate:,.2f} divine each")

    # Fetch trade history (most likely to 429 — don't let it block listings)
    fetched = []
    print(f"Fetching trade history for {config['league']}...")
    try:
        fetched = fetch_trades(session, config["league"])
        print(f"Fetched {len(fetched)} trades from API")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            print(f"  Trade history rate limited — skipping, will retry next run")
        else:
            raise

    seen = load_seen_trades()
    new_trades = find_new_trades(fetched, seen) if fetched else []

    # Fetch current listings
    print("Fetching current listings...")
    listings = fetch_listings(session, config["league"], config["account"])
    print(f"Found {len(listings)} active listings")

    listings_summary = None
    if listings:
        parsed_listings = [parse_listing(l, rates) for l in listings]
        listed_value = sum(l["div_equivalent"] for l in parsed_listings if isinstance(l["div_equivalent"], (int, float)))
        listings_summary = {"count": len(listings), "value": listed_value}

    # Add net new sales to fund currencies (per-currency)
    if new_trades:
        new_parsed_tmp = [parse_trade(t, rates) for t in new_trades]
        revenue_by_currency = {}
        for t in new_parsed_tmp:
            cur = t.get("currency", "divine")
            amt = t.get("sale_price", 0)
            if cur and amt > 0:
                revenue_by_currency[cur] = revenue_by_currency.get(cur, 0) + amt
        if revenue_by_currency:
            print(f"\nAdding trade revenue from {len(new_parsed_tmp)} new sale(s):")
            for cur, amt in revenue_by_currency.items():
                currencies[cur] = currencies.get(cur, 0) + amt
                print(f"  +{amt:,.0f} {cur}")

    # Recalculate NAV with fresh data
    fresh_listed = listings_summary["value"] if listings_summary else 0
    nav = calc_nav(currencies, rates, fresh_listed)

    # Print summary
    if new_trades:
        new_trades.sort(key=lambda t: t.get("time", ""), reverse=True)
        print_summary(new_trades, listings_summary)
    else:
        print("No new trades since last run.")
        if listings_summary:
            print(f"\nCurrent listings: {listings_summary['count']} items, ~{listings_summary['value']:,.0f} divine listed value")

    raw_divines = currencies_to_divine(currencies, rates)
    print(f"\nCurrencies:")
    for cur, amt in currencies.items():
        if amt != 0:
            print(f"  {cur}: {amt:,.0f}")
    print(f"Liquid (divine equivalent): {raw_divines:,.0f}")
    print(f"Listed value: {fresh_listed:,.0f} divine")
    print(f"Adjusted NAV (with {int((1-HAIRCUT)*100)}% haircut): {nav:,.0f} divine")
    print(f"Raw NAV (no haircut): {raw_divines + fresh_listed:,.0f} divine")

    if args.dry_run:
        print("\n(dry run — nothing saved)")
        return

    # Save trades
    if new_trades:
        new_parsed = [parse_trade(t, rates) for t in new_trades]
        save_trades(new_parsed + seen)

        csv_path = DATA_DIR / "new_trades.csv"
        export_csv(new_trades, csv_path)
        print(f"\nExported to {csv_path}")

    # Save currencies back to investors.json
    inv_data["fund"]["currencies"] = currencies
    save_investors(inv_data)

    # Build and save dashboard
    all_trades = (([parse_trade(t, rates) for t in new_trades] + seen) if new_trades else seen)
    dashboard = build_dashboard(all_trades, listings, currencies, rates, inv_data)
    save_dashboard(dashboard)
    print(f"Dashboard data saved to {DASHBOARD_FILE}")

    # Push to sheets if requested
    if args.sheets and new_trades:
        push_to_sheets(new_trades, config)

    # Git push if requested
    if args.push:
        git_push()


if __name__ == "__main__":
    main()
