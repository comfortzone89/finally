# Market Data Interface Design

The unified Python interface FinAlly uses to retrieve stock prices. Two
implementations — the **GBM simulator** and the **Massive API poller** — sit
behind one abstract base class. A factory picks the implementation at startup
based on `MASSIVE_API_KEY`. Everything downstream (SSE streaming, portfolio
valuation, trade execution) reads from a shared in-memory cache and is therefore
**source-agnostic**.

```
                       create_market_data_source(cache)
                                    │
                MASSIVE_API_KEY set?│
                  ┌─────────────────┴─────────────────┐
                  ▼                                     ▼
        MassiveDataSource                       SimulatorDataSource
   (REST poll, real prices)                  (GBM, no key needed)
                  │                                     │
                  └──────────────► PriceCache ◄─────────┘
                                  (thread-safe)
                                       │
                ┌──────────────────────┼──────────────────────┐
                ▼                       ▼                       ▼
        SSE /api/stream/prices   portfolio valuation     trade execution
```

This is the **shipped design** (`backend/app/market/`). Module names below match
the code.

---

## 1. Module Layout

```
backend/app/market/
├── __init__.py          # public exports
├── models.py            # PriceUpdate dataclass
├── interface.py         # MarketDataSource ABC
├── cache.py             # PriceCache (thread-safe store + version counter)
├── factory.py           # create_market_data_source()
├── seed_prices.py       # simulator constants (see MARKET_SIMULATOR.md)
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── massive_client.py    # MassiveDataSource (see MASSIVE_API.md)
└── stream.py            # SSE endpoint factory
```

**Public surface** (`from app.market import ...`): `PriceUpdate`, `PriceCache`,
`MarketDataSource`, `create_market_data_source`, `create_stream_router`.

---

## 2. Core Data Model — `PriceUpdate`

The only object that leaves the market layer. Immutable so it can be shared
across threads/coroutines without copying.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float: ...           # price - previous_price (4dp)
    @property
    def change_percent(self) -> float: ...   # % move vs previous (4dp; 0 if prev==0)
    @property
    def direction(self) -> str: ...          # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...           # JSON-ready, used by the SSE layer
```

Notes:

- `change`, `change_percent`, and `direction` are **derived** — not stored — so
  they can never drift out of sync with `price`/`previous_price`.
- `previous_price` is the *prior cached price for this ticker*, i.e. it drives
  the green/red tick flash. It is **not** the previous daily close. (Day-over-day
  change is a separate concern computed by the portfolio/watchlist layer from
  Massive's `prevDay.close` or the simulator's seed price.)
- `timestamp` is **Unix seconds**. Both sources normalize to this unit (the
  Massive poller converts the API's nanoseconds; see [`MASSIVE_API.md`](MASSIVE_API.md)).

---

## 3. Abstract Interface — `MarketDataSource`

A producer that pushes prices into the cache on its own schedule. It does **not**
return prices to callers — consumers always read from `PriceCache`. This keeps
both implementations interchangeable and decouples produce-rate from read-rate.

```python
class MarketDataSource(ABC):
    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Start the background task. Call exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the task and release resources. Idempotent."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add to the active set. No-op if present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove from the active set and from the cache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Current actively-tracked tickers."""
```

**Contract guarantees** every implementation must honor:

- `start()` seeds the cache with at least one price per ticker *before* the
  first interval elapses, so the SSE stream and portfolio valuation have data
  immediately (no empty-watchlist flash on first load).
- `add_ticker` / `remove_ticker` accept any case; symbols are upper-cased.
- `remove_ticker` also calls `cache.remove(ticker)` so stale prices don't linger.
- `stop()` is safe to call when never started or already stopped.

---

## 4. Shared Store — `PriceCache`

Thread-safe because the Massive client runs in a worker thread
(`asyncio.to_thread`) while readers run on the event loop. A monotonic
**version counter** lets the SSE layer detect "did anything change?" without
diffing dictionaries.

```python
class PriceCache:
    def update(self, ticker, price, timestamp=None) -> PriceUpdate:
        # computes previous_price from prior entry, rounds to 2dp,
        # stores, and bumps self._version
    def get(self, ticker) -> PriceUpdate | None
    def get_price(self, ticker) -> float | None
    def get_all(self) -> dict[str, PriceUpdate]    # shallow copy
    def remove(self, ticker) -> None
    @property
    def version(self) -> int
    # __len__, __contains__ also supported
```

Design points:

- **Single writer at a time** (one source is active), many readers. A `Lock`
  guards every mutation and the `get_all()` snapshot copy.
- **First update for a ticker** sets `previous_price == price` → `direction
  "flat"`, avoiding a spurious tick on initial load.
- **Prices rounded to 2 dp** on write, so every consumer sees the same value.
- The cache is the **single source of truth**. Trade execution reads
  `cache.get_price(ticker)` for fill price; portfolio valuation reads
  `cache.get_all()`. They never touch a data source directly. This is also what
  makes future multi-user support a non-event — add a user dimension upstream;
  the cache layer is unchanged.

---

## 5. Factory — Source Selection

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    return SimulatorDataSource(price_cache=price_cache)
```

- `.strip()` means a present-but-empty `MASSIVE_API_KEY=` falls through to the
  simulator (matches the PLAN's "absent or empty → simulator" rule).
- Returns an **unstarted** source; the caller awaits `start(tickers)`.
- Imports are top-level (both deps are always installed), so a typo in one
  module surfaces at import time, not first request.

---

## 6. SSE Integration — `create_stream_router`

The streaming endpoint is a thin reader over the cache. The factory injects the
`PriceCache` so there are no module globals.

```python
router = create_stream_router(price_cache)   # GET /api/stream/prices
app.include_router(router)
```

Behavior of the generator (`_generate_events`):

1. Emits `retry: 1000` first so `EventSource` auto-reconnects after 1 s on drop.
2. Every ~500 ms, compares `cache.version` to the last seen version.
3. **Only emits when the version changed** — no redundant frames when prices are
   static. Payload is `data: {"AAPL": {...to_dict()...}, ...}\n\n`.
4. Breaks the loop when `await request.is_disconnected()` is true, and handles
   `asyncio.CancelledError` on shutdown.

Headers set: `Cache-Control: no-cache`, `Connection: keep-alive`,
`X-Accel-Buffering: no` (defeats nginx/proxy buffering).

---

## 7. Implementation Sketches

### Massive (`MassiveDataSource`)

```python
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key, price_cache, poll_interval=15.0): ...

    async def start(self, tickers):
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()                       # immediate data
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_once(self):
        snaps = await asyncio.to_thread(self._fetch_snapshots)   # blocking → thread
        for snap in snaps:
            self._cache.update(snap.ticker, snap.last_trade.price,
                               timestamp=snap.last_trade.timestamp / 1e9)  # ns → s
```

Poll interval defaults to 15 s (free tier). One `get_snapshot_all` call covers
the whole watchlist. Added tickers appear on the next poll; failures are logged
and swallowed so the loop survives. See [`MASSIVE_API.md`](MASSIVE_API.md).

### Simulator (`SimulatorDataSource`)

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache, update_interval=0.5, event_probability=0.001): ...

    async def start(self, tickers):
        self._sim = GBMSimulator(tickers, event_probability=self._event_prob)
        for t in tickers:                              # seed cache immediately
            self._cache.update(t, self._sim.get_price(t))
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self):
        while True:
            for ticker, price in self._sim.step().items():
                self._cache.update(ticker, price)
            await asyncio.sleep(self._interval)
```

Ticks every 500 ms. See [`MARKET_SIMULATOR.md`](MARKET_SIMULATOR.md).

---

## 8. Application Lifecycle

```python
# --- startup (FastAPI lifespan) ---
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(initial_watchlist_tickers)
app.include_router(create_stream_router(cache))

# --- during runtime ---
await source.add_ticker("TSLA")       # on POST /api/watchlist
await source.remove_ticker("GOOGL")   # on DELETE /api/watchlist/{ticker}
price = cache.get_price("AAPL")       # trade fill / valuation

# --- shutdown ---
await source.stop()
```

Hold `cache` and `source` on `app.state` (or a small context object) so route
handlers can reach them. The watchlist routes must update **both** the database
and the live `source` so the stream reflects changes without a restart.

---

## 9. Why This Shape

| Choice | Rationale |
|--------|-----------|
| Producer writes to a cache (vs. returning prices) | Decouples poll/tick cadence from read/stream cadence; one slow source can't stall readers. |
| Single `PriceUpdate` leaving the layer | Downstream code learns one type; switching sources changes nothing above the cache. |
| Version counter on the cache | SSE sends frames only on real change — cheap idle, no diffing. |
| Derived `change`/`direction` | Impossible to store an inconsistent tick. |
| Factory keyed on env var | Same binary runs in demo (simulator) and live (Massive) modes with zero code changes. |
| Threaded Massive calls | The official client is synchronous; threading keeps the event loop responsive. |
