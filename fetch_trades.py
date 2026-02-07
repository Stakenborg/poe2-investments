#!/usr/bin/env python3
"""Fetch PoE2 trade history + current listings, generate dashboard data."""

import argparse
import csv
import json
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

        if c1_id == "divine" and c2_id in ("exalted", "chaos", "mirror"):
            # c2 per divine = c2_price / c1_price... we want divine per c2
            rates[c2_id] = c2_price / c1_price if c1_price else 0
        elif c2_id == "divine" and c1_id in ("exalted", "chaos", "mirror"):
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


def build_dashboard(trades, listings, raw_divines, rates):
    parsed_listings = [parse_listing(l, rates) for l in listings]
    listed_value = sum(
        l["div_equivalent"] for l in parsed_listings
        if isinstance(l["div_equivalent"], (int, float))
    )

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "raw_divines": raw_divines,
        "listed_value": listed_value,
        "total_nav": raw_divines + listed_value,
        "exchange_rates": rates,
        "listings": parsed_listings,
        "recent_sales": trades[:50],  # Last 50 sales for the dashboard
    }


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


def git_push():
    """Commit and push dashboard.json to the remote."""
    repo_root = Path(__file__).parent
    try:
        subprocess.run(
            ["git", "add", "data/dashboard.json"],
            cwd=repo_root, check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "status", "--porcelain", "data/dashboard.json"],
            cwd=repo_root, check=True, capture_output=True, text=True,
        )
        if not result.stdout.strip():
            print("No data changes to push.")
            return
        subprocess.run(
            ["git", "commit", "-m", "Update dashboard data"],
            cwd=repo_root, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=repo_root, check=True, capture_output=True,
        )
        print("Pushed dashboard data to remote.")
    except subprocess.CalledProcessError as e:
        print(f"Git push failed: {e.stderr.decode().strip() if e.stderr else e}")


def main():
    parser = argparse.ArgumentParser(description="Fetch PoE2 trade history")
    parser.add_argument("--sheets", action="store_true", help="Push new trades to Google Sheets")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and display without saving")
    parser.add_argument("--divines", type=float, help="Update raw divine count in the fund")
    parser.add_argument("--push", action="store_true", help="Git commit + push dashboard data after update")
    args = parser.parse_args()

    config = get_config()
    session = make_session(config["poesessid"])

    # Load previous dashboard for persisted values
    prev_dashboard = load_dashboard()
    raw_divines = args.divines if args.divines is not None else prev_dashboard.get("raw_divines", 0)

    # Fetch exchange rates
    print("Fetching exchange rates...")
    rates = fetch_exchange_rates(config["league"])
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

    # Print summary
    if new_trades:
        new_trades.sort(key=lambda t: t.get("time", ""), reverse=True)
        print_summary(new_trades, listings_summary)
    else:
        print("No new trades since last run.")
        if listings_summary:
            print(f"\nCurrent listings: {listings_summary['count']} items, ~{listings_summary['value']:,.0f} divine listed value")

    print(f"\nRaw divines: {raw_divines:,.0f}")
    nav = raw_divines + (listings_summary["value"] if listings_summary else 0)
    print(f"Total NAV: {nav:,.0f} divine")

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

    # Build and save dashboard
    all_trades = (([parse_trade(t, rates) for t in new_trades] + seen) if new_trades else seen)
    dashboard = build_dashboard(all_trades, listings, raw_divines, rates)
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
