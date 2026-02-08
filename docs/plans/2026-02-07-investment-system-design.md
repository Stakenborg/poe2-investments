# The Vault — Investment System Design

## Overview

A lightweight investment system for "The Vault," a PoE2 crafting fund shared among ~5 trusted friends. Investors deposit divines, receive units priced against the fund's NAV, and can withdraw at any time. All management is done via CLI; investors interact through a static site with personalized views and Discord-integrated request flows.

No server, no database, no auth. Just a static GitHub Pages site, a Python script, and a Discord webhook.

---

## NAV Calculation

The fund's Net Asset Value determines unit pricing and investor positions.

```
NAV = liquid_divines + (listed_value * 0.85)
```

- **Liquid**: divines held in-game, not invested in items
- **Listed value**: total market value of all items currently listed for sale
- **85% haircut**: listed items are valued at 85% of their listing price to account for the reality that not everything sells at asking price
- **Unit price**: `NAV / total_units`

### Display

The hero section shows the **adjusted NAV** (with haircut) as the primary number. An asterisk footnote shows the raw listed value and explains the 15% haircut:

> *Listed value: 412 div. Adjusted NAV applies a 15% haircut to listed items.*

---

## Unit Pricing

- Fund launches at unit price **1.00** (1 unit = 1 divine)
- All deposits and withdrawals are converted to/from units at the current unit price
- **Deposit**: `units_issued = deposit_amount / unit_price`
- **Withdrawal**: `divines_returned = units_redeemed * unit_price`

The manager's own capital works identically — same unit pricing, same rules.

---

## Deposits

- Open anytime, no minimum
- An investor can be created and receive their first deposit in one operation
- If the investor doesn't exist, the CLI confirms before creating them
- Each deposit:
  1. Calculates current unit price
  2. Issues new units: `amount / unit_price`
  3. Adds to `total_units`
  4. Records transaction in investor history

---

## Withdrawals

### Request Flow

1. Investor visits their personalized view on the site (`?code=xyz123`)
2. Fills out withdrawal form (amount in divines, capped at their position value)
3. Form POSTs to a Discord webhook — manager gets a notification:
   > **Grimlock** requests withdrawal of **45 div**
   > Unit price at request: **1.23**
   > *2026-02-07 14:32 UTC*
4. Request is stored as pending with the **locked unit price** from submission time
5. Shows on investor's personalized view as "Pending — requested 45 div"

### Fulfillment

Manager runs `--fulfill "Grimlock"` which:

1. Uses the **locked price** from request time (not current price) — prevents dilution issues from processing order
2. Calculates performance fee if applicable
3. Burns units: `amount / locked_price`
4. Reduces `total_units`
5. Prints summary:
   > Fulfilling withdrawal for Grimlock: 45 div
   > Performance fee: 3.2 div (25% on gains above HWM 1.00)
   > Net to investor: 41.8 div
6. Manager delivers the net amount in-game

### Deposits via Site

Same flow as withdrawals — form on personalized view, Discord notification, manager fulfills via CLI. Unit price locked at request time.

---

## Performance Fee

- **25% of gains** above the high-water mark (HWM)
- HWM starts at the initial unit price (1.00)
- Fee is calculated per-withdrawal on the gain portion only:
  - Investor deposited at 1.00, withdraws at 1.40
  - Gain: 0.40/unit, fee: 0.10/unit, net: 1.30/unit
- HWM updates to the new peak after fee is assessed
- If fund drops and recovers to the same level, no new fee (HWM not exceeded)
- Fee is **not auto-collected** — the script calculates and prints it, manager handles collection manually
- No management fee

---

## Invite Codes

- Generated via `--gen-code "Grimlock"` — produces a random shared secret
- Plaintext code stored in private `data/investors.json` (gitignored)
- SHA-256 hash of the code stored in public `dashboard.json`
- Investor accesses their personalized view via `?code=xyz123`
- Client-side validation: hash the URL param, match against hashed codes in dashboard.json
- No match = normal public view, no error, no indication codes exist

---

## Cap Table

Displayed publicly at the bottom of the dashboard.

| Investor | Value (div) | Share | Profit |
|----------|------------|-------|--------|
| Stake (manager icon) | 312 div | 52.0% | +87 div |
| Grimlock | 144 div | 24.0% | +19 div |
| Frostbite | 84 div | 14.0% | +9 div |
| Nyx | 60 div | 10.0% | -2 div |

Below the table, a standalone summary bar:

> **Fund Total: 600 div** | **+113 div profit**

- **Value** = investor's units * current unit price
- **Profit** = current value - total deposited (green if positive, red if negative)
- Manager row tagged with an icon
- Footer: *"25% performance fee on gains above high-water mark"*

---

## Personalized Investor View

When visiting with a valid invite code (`?code=xyz123`):

### Position Card (hero area)
Prominent card below the fund value showing:
- Their value (div), fund share (%), profit

### Transaction History
Table of all deposits and withdrawals with date, amount, and unit price at time of transaction.

### Pending Requests
If an unfulfilled request exists: status display with type, amount, and date.

### Request Form
- Type toggle: Deposit / Withdraw
- Amount field (divines), withdraw capped at position value
- Submit POSTs to Discord webhook with amount and locked unit price
- Confirmation message on success

### Cap Table Highlight
Their row in the public cap table gets a subtle highlight so they can spot themselves.

---

## Data Model

### `dashboard.json` (public, committed to git)

```json
{
  "fund": {
    "total_units": 487.5,
    "unit_price": 1.23,
    "hwm": 1.20,
    "haircut": 0.85,
    "perf_fee_pct": 0.25,
    "total_deposited": 487,
    "total_profit": 113,
    "discord_webhook": "https://discord.com/api/webhooks/..."
  },
  "investors": [
    {
      "name": "Grimlock",
      "hash": "a1b2c3...",
      "units": 117.07,
      "deposited": 125,
      "value": 144,
      "share": 0.24,
      "profit": 19,
      "pending": { "type": "withdraw", "amount": 45, "date": "2026-02-07", "locked_price": 1.23 },
      "history": [
        { "type": "deposit", "amount": 100, "date": "2026-01-15", "unit_price": 1.00 },
        { "type": "deposit", "amount": 25, "date": "2026-01-22", "unit_price": 1.07 }
      ]
    }
  ]
}
```

### `data/investors.json` (private, gitignored)

Same structure as above but includes plaintext `code` field for each investor. Source of truth for invite codes.

---

## CLI Commands

All via `fetch_trades.py`:

| Command | Description |
|---------|-------------|
| `--add-investor "Name" --deposit 100` | Create investor + first deposit (confirms if new) |
| `--deposit "Name" 50` | Additional deposit for existing investor |
| `--withdraw "Name" 45` | Create withdrawal request at current unit price |
| `--fulfill "Name"` | Process pending request using locked price |
| `--gen-code "Name"` | Generate invite code, print plaintext, store hash |

Each command recalculates NAV, updates unit prices and all investor values/shares/profits, writes both JSON files, and optionally pushes to GitHub (with `--push`).

---

## What Stays the Same

- All existing dashboard functionality (hero, items, modals, scroll animations)
- `fetch_trades.py` data fetching and rate-limit handling
- GitHub Pages hosting and `--push` deploy flow
- Dark fintech theme, gold accents, existing font stack
