# PoE2 Trade Fetcher — Design

## Goal

Pull trade history from the PoE2 trade site, de-duplicate against prior runs, and output new sales for fund tracking.

## Endpoint

- `GET https://www.pathofexile.com/api/trade2/history/{league}`
- Auth: `POESESSID` cookie from browser session
- Returns JSON with `result` array of completed trades

## Data Per Trade

| Field | Source |
|-------|--------|
| Timestamp | `time` |
| Item Name | `item.name` or `item.typeLine` |
| Base Type | `item.baseType` |
| Rarity | `item.rarity` |
| Sale Price | `price.amount` |
| Currency | `price.currency` |
| Div Equivalent | amount if divine, blank otherwise |
| Item ID | `item_id` (de-dup key) |

## De-duplication

- `data/trades.json` stores all previously seen trades (parsed format)
- `item_id` is the unique key.
- New trades are prepended (most recent first).

## Output

- **Default:** CSV at `data/new_trades.csv` (overwritten each run, only new trades).
- **`--sheets`:** Insert rows at top of Google Sheets "Sales" tab (row 2, below header).
- **`--dry-run`:** Fetch and display, no save.

## Config

`.env` file with: `POESESSID`, `LEAGUE`, `SHEET_ID`, `GOOGLE_CREDENTIALS`, `SALES_TAB`.

## Exchange Rates

Not handled yet. Non-divine currencies stored as-is, div-equivalent left blank for manual conversion.
