# Market Simulator Design

Approach and code structure for FinAlly's built-in stock-price simulator — the
default data source when no `MASSIVE_API_KEY` is configured. It produces a
continuous, realistic-looking price stream with **no external dependencies**, so
the app is fully functional offline and in CI.

Lives in `backend/app/market/simulator.py` (the `GBMSimulator` engine plus the
`SimulatorDataSource` wrapper) with constants in `seed_prices.py`. It implements
the `MarketDataSource` interface from [`MARKET_INTERFACE.md`](MARKET_INTERFACE.md).

---

## 1. Model — Geometric Brownian Motion

GBM is the standard model behind Black-Scholes. Prices evolve multiplicatively
with random noise, so they **can never go negative** and exhibit the lognormal
distribution seen in real markets. Each tick:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt  +  σ·√dt·Z )
```

| Symbol | Meaning | Example |
|--------|---------|---------|
| `S(t)` | current price | 190.00 |
| `μ` | annualized drift (expected return) | 0.05 (5%) |
| `σ` | annualized volatility | 0.22 (22%) |
| `dt` | time step as a fraction of a trading year | ~8.5e-8 |
| `Z` | correlated standard-normal draw, N(0,1) | — |

### Choosing `dt`

500 ms ticks, over a market year of 252 trading days × 6.5 h/day:

```
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   = 5,896,800
dt = 0.5 / 5,896,800                          ≈ 8.48e-8
```

This tiny `dt` yields **sub-cent moves per tick** that accumulate naturally —
the price "breathes" instead of jumping. Over a simulated trading day the
intraday range lands in a believable band for each ticker's `σ`.

---

## 2. Correlated Moves (Cholesky)

Real stocks don't move independently — tech names rise and fall together. The
simulator draws **correlated** normals so the dashboard looks like a real market
(sectors moving in sympathy) rather than uncorrelated noise.

Given a correlation matrix `C`, compute its Cholesky factor `L = cholesky(C)`.
For a vector of independent normals `z`:

```
z_correlated = L @ z          # has covariance C
```

The matrix is rebuilt whenever tickers are added/removed (`O(n²)`, negligible
for n < 50).

### Correlation structure (`seed_prices.py`)

| Relationship | ρ | Constant |
|--------------|---|----------|
| Tech ↔ Tech | 0.6 | `INTRA_TECH_CORR` |
| Finance ↔ Finance | 0.5 | `INTRA_FINANCE_CORR` |
| TSLA ↔ anything | 0.3 | `TSLA_CORR` |
| Cross-sector / unknown | 0.3 | `CROSS_GROUP_CORR` |

```python
CORRELATION_GROUPS = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}
```

TSLA is listed in the tech set but is special-cased to 0.3 against everything —
it "does its own thing." A correlation matrix built from these rules is positive
semi-definite, so `numpy.linalg.cholesky` always succeeds.

---

## 3. Random Events

Each tick, every ticker has a small probability (`event_probability = 0.001`) of
a sudden **2–5% shock** in a random direction — for visual drama.

```python
if random.random() < self._event_prob:
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    price *= 1 + shock_magnitude * shock_sign
```

At 0.1% per tick, ~2 ticks/sec, 10 tickers → an event roughly **every ~50
seconds** somewhere on the board. Enough to keep it interesting, rare enough to
stay believable.

---

## 4. Seed Prices & Per-Ticker Parameters

Realistic starting prices and individually tuned volatility/drift
(`seed_prices.py`):

```python
SEED_PRICES = {
    "AAPL": 190.0,  "GOOGL": 175.0, "MSFT": 420.0, "AMZN": 185.0, "TSLA": 250.0,
    "NVDA": 800.0,  "META": 500.0,  "JPM": 195.0,  "V": 280.0,    "NFLX": 600.0,
}

TICKER_PARAMS = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # high vol
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # high vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}   # for dynamically-added tickers
```

A ticker added at runtime that isn't in `SEED_PRICES` starts at a random price
in **$50–$300** and uses `DEFAULT_PARAMS`.

---

## 5. Engine — `GBMSimulator`

Pure, synchronous price engine. No asyncio, no cache — just math. This makes it
trivially unit-testable.

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

    def __init__(self, tickers, dt=DEFAULT_DT, event_probability=0.001): ...

    def step(self) -> dict[str, float]:
        """Advance every ticker one tick. Returns {ticker: price}. Hot path."""
        n = len(self._tickers)
        if n == 0:
            return {}
        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z                  # correlate
        out = {}
        for i, t in enumerate(self._tickers):
            mu, sigma = self._params[t]["mu"], self._params[t]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[t] *= math.exp(drift + diffusion)
            if random.random() < self._event_prob:  # random shock
                self._prices[t] *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])
            out[t] = round(self._prices[t], 2)
        return out

    def add_ticker(self, ticker): ...      # seed price + params, rebuild Cholesky
    def remove_ticker(self, ticker): ...   # drop state, rebuild Cholesky
    def get_price(self, ticker) -> float | None
    def get_tickers(self) -> list[str]
```

Internals:

- `_add_ticker_internal()` adds state **without** rebuilding Cholesky, so the
  constructor can batch-add all seed tickers and rebuild **once** at the end.
- `_rebuild_cholesky()` builds the `n×n` correlation matrix from
  `_pairwise_correlation()` and factors it; sets `None` for `n ≤ 1` (no
  correlation needed).
- `_pairwise_correlation()` is a static method that maps a ticker pair to ρ
  using the rules in §2.

---

## 6. Async Wrapper — `SimulatorDataSource`

Adapts the engine to the `MarketDataSource` interface: a background asyncio task
that steps the simulator and writes into the `PriceCache`.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache, update_interval=0.5, event_probability=0.001): ...

    async def start(self, tickers):
        self._sim = GBMSimulator(tickers, event_probability=self._event_prob)
        for t in tickers:                              # seed cache up front
            self._cache.update(t, self._sim.get_price(t))
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def _run_loop(self):
        while True:
            try:
                for ticker, price in self._sim.step().items():
                    self._cache.update(ticker, price)
            except Exception:
                logger.exception("Simulator step failed")   # never kill the loop
            await asyncio.sleep(self._interval)

    async def add_ticker(self, ticker):    # sim.add_ticker + seed cache immediately
    async def remove_ticker(self, ticker): # sim.remove_ticker + cache.remove
    async def stop(self):                  # cancel task, await CancelledError
    def get_tickers(self): return self._sim.get_tickers() if self._sim else []
```

Key behaviors:

- **Immediate seeding** in `start()` and `add_ticker()` — the cache (and thus the
  SSE stream and portfolio valuation) has a price for every ticker before the
  first 500 ms tick, so the UI never shows a blank.
- The loop **catches and logs** per-step exceptions instead of dying, so a single
  bad tick can't kill the stream.
- `stop()` cancels the task and awaits the `CancelledError` for clean shutdown.

---

## 7. File Structure

```
backend/app/market/
├── seed_prices.py   # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS,
│                    # CORRELATION_GROUPS + correlation constants
└── simulator.py     # GBMSimulator (engine) + SimulatorDataSource (async wrapper)
```

Constants are isolated in `seed_prices.py` so they can be tuned without touching
engine logic.

---

## 8. Behavior Notes & Tuning

- **No negative prices** — GBM is multiplicative (`exp()` is always positive).
- **Sub-cent per-tick moves** from the tiny `dt`; realistic intraday range emerges
  over minutes, not in a single jump.
- **`σ` controls liveliness** — TSLA (0.50) visibly jumps; JPM/V (~0.17) drift
  calmly. Raise `σ` globally for a more dramatic demo.
- **`event_probability`** is the drama dial — raise it for more frequent shocks,
  set to 0 for a calm, pure-GBM stream.
- **Correlation matrix must stay PSD.** The rule-based ρ values produce a valid
  matrix; if you hand-edit correlations, keep them consistent or `cholesky` will
  raise `LinAlgError`.
- **Determinism for tests** — seed `random` and `numpy.random` to make `step()`
  reproducible; the engine's purity (no I/O) makes this clean.
- **Adding tickers mid-session** rebuilds Cholesky (`O(n²)`), negligible for the
  expected handful of watchlist symbols.

---

## 9. Quick Demo

A Rich terminal dashboard exercises the simulator directly:

```bash
cd backend
uv run market_data_demo.py
```

It renders all 10 tickers with sparklines, color-coded direction arrows, and an
event log — a fast way to eyeball the simulator's feel after tuning constants.
