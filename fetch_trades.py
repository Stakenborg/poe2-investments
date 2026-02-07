#!/usr/bin/env python3
"""Fetch PoE2 trade history and export new sales."""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
import os

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
TRADES_FILE = DATA_DIR / "trades.json"

TRADE_URL = "https://www.pathofexile.com/api/trade2/history/{league}"


def get_config():
    poesessid = os.getenv("POESESSID")
    if not poesessid:
        print("Error: POESESSID not set in .env file")
        sys.exit(1)
    return {
        "poesessid": poesessid,
        "league": os.getenv("LEAGUE", "Fate of the Vaal"),
        "sheet_id": os.getenv("SHEET_ID", ""),
        "google_credentials": os.getenv("GOOGLE_CREDENTIALS", "credentials.json"),
        "sales_tab": os.getenv("SALES_TAB", "Sales"),
    }


def fetch_trades(poesessid, league):
    url = TRADE_URL.format(league=quote(league))
    cookies = {"POESESSID": poesessid}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    resp = requests.get(url, cookies=cookies, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", [])


def load_seen_trades():
    if TRADES_FILE.exists():
        with open(TRADES_FILE) as f:
            return json.load(f)
    return []


def save_trades(trades):
    DATA_DIR.mkdir(exist_ok=True)
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def parse_trade(trade):
    item = trade.get("item", {})
    price = trade.get("price", {})
    name = item.get("name") or item.get("typeLine") or "Unknown"
    base_type = item.get("baseType", "")
    currency = price.get("currency", "")
    amount = price.get("amount", 0)
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
    }


def find_new_trades(fetched, seen):
    seen_ids = {t["item_id"] for t in seen}
    new = []
    for trade in fetched:
        item_id = trade.get("item_id", "")
        if item_id and item_id not in seen_ids:
            new.append(trade)
    return new


def export_csv(trades, path):
    rows = [parse_trade(t) for t in trades]
    if not rows:
        return
    fieldnames = ["timestamp", "item_name", "base_type", "rarity", "sale_price", "currency", "div_equivalent"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
        # Insert at row 2 (after header), most recent first
        worksheet.insert_rows(rows, row=2)
        print(f"Pushed {len(rows)} rows to Google Sheets")


def print_summary(new_trades):
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


def main():
    parser = argparse.ArgumentParser(description="Fetch PoE2 trade history")
    parser.add_argument("--sheets", action="store_true", help="Push new trades to Google Sheets")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and display without saving")
    args = parser.parse_args()

    config = get_config()

    print(f"Fetching trade history for {config['league']}...")
    fetched = fetch_trades(config["poesessid"], config["league"])
    print(f"Fetched {len(fetched)} trades from API")

    seen = load_seen_trades()
    new_trades = find_new_trades(fetched, seen)

    if not new_trades:
        print("No new trades since last run.")
        return

    # Sort newest first
    new_trades.sort(key=lambda t: t.get("time", ""), reverse=True)

    print_summary(new_trades)

    if args.dry_run:
        print("\n(dry run — nothing saved)")
        return

    # Parse new trades and prepend to existing (most recent first)
    new_parsed = [parse_trade(t) for t in new_trades]
    save_trades(new_parsed + seen)

    # Export CSV
    csv_path = DATA_DIR / "new_trades.csv"
    export_csv(new_trades, csv_path)
    print(f"\nExported to {csv_path}")

    # Push to sheets if requested
    if args.sheets:
        push_to_sheets(new_trades, config)


if __name__ == "__main__":
    main()
