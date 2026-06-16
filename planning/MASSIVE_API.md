# Massive API Reference (formerly Polygon.io)

Reference for the Massive market-data REST API as used in FinAlly to retrieve
real-time and end-of-day stock prices for multiple tickers in a single call.

> **Rebrand note.** Polygon.io became **Massive** on **30 October 2025**. The
> SDK now defaults to `https://api.massive.com`; the legacy
> `https://api.polygon.io` base and existing Polygon API keys continue to work.
> The Python package, classes, and method names are unchanged from the
> `polygon-api-client` era — only the brand and default host moved.

---

## 1. At a Glance

| Item | Value |
|------|-------|
| REST base URL | `https://api.massive.com` (legacy `https://api.polygon.io` still works) |
| Python package | `massive` — `uv add massive` / `pip install -U massive` |
| Client class | `massive.RESTClient` (synchronous) |
| Min Python | 3.9+ |
| Auth | `Authorization: Bearer <API_KEY>` (the client sets this for you) |
| Primary endpoint for FinAlly | Multi-ticker stocks snapshot (one call, all watched tickers) |
| Timestamp unit (snapshots) | **Unix nanoseconds** |

### Rate Limits

| Tier | Limit | FinAlly poll cadence |
|------|-------|----------------------|
| Free | 5 requests / minute | every **15 s** (default) |
| Paid (Starter → Enterprise) | "Unlimited" — recommended < 100 req/s | every **2–5 s** |

Because FinAlly fetches **all watched tickers in a single snapshot call**, one
request per poll covers the whole watchlist. The free tier's 5 req/min is
therefore comfortable at a 15 s interval (4 req/min).

---

## 2. Client Initialization

```python
from massive import RESTClient

# Pass the key explicitly (how FinAlly does it — from MASSIVE_API_KEY env var)
client = RESTClient(api_key="your_key_here")
```

The constructor also accepts `num_pools`, `retries` (default 3, with backoff),
`timeout`, `connect_timeout`, and `read_timeout`. FinAlly relies on the default
retry behavior for transient 5xx/network errors.

> **Threading.** `RESTClient` is **synchronous** (blocking `requests`). FinAlly
> runs every call through `asyncio.to_thread(...)` so the FastAPI event loop is
> never blocked. See `backend/app/market/massive_client.py`.

---

## 3. Primary Endpoint — Multi-Ticker Snapshot

Returns the latest market data for a set of tickers in **one** request. This is
the workhorse of FinAlly's poller.

**REST**

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

| Query param | Type | Notes |
|-------------|------|-------|
| `tickers` | string | **Case-sensitive**, comma-separated (`AAPL,TSLA,GOOG`). Omit to get the entire market (10,000+ tickers). |
| `include_otc` | bool | Include OTC securities. Default `false`. |

**Python client**

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=api_key)

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,   # or the string "stocks"
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  day change: {snap.todays_change_percent:.2f}%")
    print(f"  prev close: ${snap.prev_day.close}")
    print(f"  day OHLC: O={snap.day.open} H={snap.day.high} "
          f"L={snap.day.low} C={snap.day.close} V={snap.day.volume}")
```

### Raw JSON response

```json
{
  "status": "OK",
  "count": 1,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": -4.54,
      "todaysChangePerc": -3.50,
      "updated": 1675190399999999999,
      "day":     { "o": 129.61, "h": 130.15, "l": 125.07, "c": 125.07, "v": 111237700, "vw": 127.35 },
      "prevDay": { "o": 131.25, "h": 133.51, "l": 128.69, "c": 129.61, "v": 99889400,  "vw": 130.42 },
      "min":     { "o": 125.10, "h": 125.20, "l": 125.00, "c": 125.07, "v": 51200, "vw": 125.08, "t": 1675190340000 },
      "lastTrade": { "p": 125.07, "s": 100, "x": 11, "c": [12], "t": 1675190399999999999, "i": "1" },
      "lastQuote": { "p": 125.06, "s": 500, "P": 125.08, "S": 1000, "t": 1675190399500000000 }
    }
  ]
}
```

### Field map (JSON → Python attribute)

| JSON | Python (`TickerSnapshot`) | Meaning |
|------|---------------------------|---------|
| `ticker` | `snap.ticker` | Symbol |
| `lastTrade.p` | `snap.last_trade.price` | **Current price** (what FinAlly trades/display on) |
| `lastTrade.s` | `snap.last_trade.size` | Trade size |
| `lastTrade.t` | `snap.last_trade.timestamp` | SIP timestamp, **nanoseconds** |
| `lastQuote.p` / `lastQuote.P` | `snap.last_quote.bid_price` / `.ask_price` | NBBO bid / ask |
| `day.{o,h,l,c,v,vw}` | `snap.day.{open,high,low,close,volume,vwap}` | Today's bar so far |
| `prevDay.c` | `snap.prev_day.close` | **Previous close** (basis for day change) |
| `min` | `snap.min` | Latest minute bar |
| `todaysChange` | `snap.todays_change` | $ change vs. previous close |
| `todaysChangePerc` | `snap.todays_change_percent` | % change vs. previous close |
| `updated` | `snap.updated` | Last update, **nanoseconds** |

> **Correction vs. earlier drafts.** Previous close is `prevDay.c`
> (`snap.prev_day.close`), **not** a `day.previous_close` field — that field does
> not exist. Day change is the top-level `todaysChange` / `todaysChangePerc`,
> not nested under `day`.

### Fields FinAlly extracts

Only two values are needed to feed the price cache:

- `snap.last_trade.price` → current price
- `snap.last_trade.timestamp` → event time (convert ns → s, see Gotchas)

Day change %, OHLC, and previous close are available for the frontend if the
watchlist endpoint wants to enrich its response, but the live stream only needs
price + timestamp.

---

## 4. Single-Ticker Snapshot

For an on-demand detail view (e.g. when the user clicks a ticker).

```python
snap = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)
print(snap.last_trade.price, snap.todays_change_percent)
```

**REST:** `GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}`

---

## 5. Gainers / Losers Snapshot (optional)

```python
movers = client.get_snapshot_direction(
    market_type=SnapshotMarketType.STOCKS,
    direction="gainers",   # or "losers"
)
```

Useful if the AI assistant wants to surface "today's biggest movers." Not part
of the core poll loop.

---

## 6. Previous Close — End-of-Day Price

Single-ticker previous-day OHLCV. Handy for seeding or for an EOD view.

**REST:** `GET /v2/aggs/ticker/{ticker}/prev`

```python
prev = client.get_previous_close_agg(ticker="AAPL")
for agg in prev:
    print(f"prev close: ${agg.close}  (O={agg.open} H={agg.high} "
          f"L={agg.low} V={agg.volume})")
```

```json
{
  "ticker": "AAPL",
  "results": [
    { "o": 150.0, "h": 155.0, "l": 149.0, "c": 154.5, "v": 1000000, "t": 1672531200000 }
  ]
}
```

> Aggregate (`/v2/aggs`) timestamps `t` are **milliseconds**, unlike snapshot
> timestamps which are nanoseconds.

---

## 7. Historical Aggregates (Bars)

For historical charts (not part of the live loop).

**REST:** `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

```python
bars = []
for a in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",        # second|minute|hour|day|week|month|quarter|year
    from_="2026-01-01",
    to="2026-06-01",
    limit=50000,
):
    bars.append(a)            # each: a.open, a.high, a.low, a.close, a.volume, a.timestamp
```

`list_aggs` auto-paginates and yields typed `Agg` objects.

---

## 8. Last Trade / Last Quote (single ticker)

```python
trade = client.get_last_trade(ticker="AAPL")
print(trade.price, trade.size)

quote = client.get_last_quote(ticker="AAPL")
print(quote.bid_price, quote.ask_price)
```

---

## 9. How FinAlly Uses the API

The Massive poller (`MassiveDataSource`) runs as a single background task:

1. Hold the union of all watched tickers.
2. Call `get_snapshot_all(STOCKS, tickers=...)` — **one** request for all tickers.
3. For each snapshot, read `last_trade.price` and `last_trade.timestamp`.
4. Write `(ticker, price, timestamp)` into the shared `PriceCache`.
5. Sleep `poll_interval` (15 s free / 2–5 s paid), repeat.

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

async def poll_loop(api_key, get_tickers, cache, interval=15.0):
    client = RESTClient(api_key=api_key)
    while True:
        tickers = get_tickers()
        if tickers:
            snaps = await asyncio.to_thread(
                client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=tickers,
            )
            for snap in snaps:
                cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    timestamp=snap.last_trade.timestamp / 1_000_000_000,  # ns → s
                )
        await asyncio.sleep(interval)
```

This matches the shipped `MarketDataSource` interface — see
[`MARKET_INTERFACE.md`](MARKET_INTERFACE.md).

---

## 10. Error Handling

| HTTP | Cause | FinAlly response |
|------|-------|------------------|
| 401 | Invalid / missing API key | Log error; poll loop retries (won't recover until key fixed) |
| 403 | Plan doesn't include the endpoint | Log error; consider falling back to simulator |
| 429 | Rate limit exceeded (free tier) | Increase `poll_interval`; client does not auto-retry 429 |
| 5xx | Server error | Client auto-retries (default 3, with backoff) |

The poller wraps each cycle in `try/except`, logs failures, and **never
re-raises** — a failed poll just means the cache keeps its last values until the
next successful cycle. The stream stays alive.

---

## 11. Gotchas

- **Snapshot timestamps are nanoseconds**, not milliseconds. Divide
  `last_trade.timestamp` and `updated` by `1_000_000_000` to get Unix seconds.
  (Aggregate endpoints `/v2/aggs/*` use **milliseconds** — divide by 1000.)
  ⚠️ The current `massive_client.py` divides by `1000.0`; with nanosecond
  inputs this yields a far-future timestamp. Treat this as a known follow-up —
  prefer dividing by `1e9`, or fall back to `time.time()` when the snapshot
  value looks out of range.
- **`tickers` is case-sensitive** — always send upper-case symbols.
- **Outside market hours**, `last_trade.price` is the last traded price (may be
  after-hours/pre-market). The `day` bar resets daily at **3:30 AM EST** and
  repopulates from ~4:00 AM EST; during that window `day.*` may be empty/zero
  while `prevDay` is still valid.
- A requested ticker that doesn't exist is simply **absent** from the response
  `tickers` array — iterate over what's returned, don't assume 1:1 with input.
- The market-wide snapshot (no `tickers` param) returns 10,000+ rows — only use
  it deliberately; for a watchlist always pass `tickers=`.

---

## Sources

- [Polygon.io is Now Massive](https://massive.com/blog/polygon-is-now-massive)
- [Full Market Snapshot — Stocks REST API](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Stocks REST API Overview](https://massive.com/docs/rest/stocks/overview)
- [massive-com/client-python (GitHub)](https://github.com/massive-com/client-python)
- [Request limits — Massive Knowledge Base](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis)
