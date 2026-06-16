# Code Review — Market Data Component

**Date:** 2026-06-16
**Scope:** `backend/app/market/` and its tests (`backend/tests/market/`), plus `planning/PLAN.md` doc conformance. This is the only feature code implemented so far.

## Verdict

The market-data subsystem is well-built: clean strategy pattern, immutable `PriceUpdate`, a single thread-safe `PriceCache` as source of truth, correct GBM math with Cholesky-correlated moves, defensive/cancellable background loops, and version-based SSE change detection. **No critical bugs.** All 7 issues from the prior review (`planning/archive/MARKET_DATA_REVIEW.md`) appear genuinely fixed. The findings below are mostly new.

---

## High

### H1 — SSE event format diverges from PLAN.md contract
`backend/app/market/stream.py:81-83` vs. PLAN.md §6.

PLAN.md says "Each SSE event contains ticker, price, previous price, timestamp, and change direction" (reads as one event per ticker). The code sends a single event containing a snapshot dict keyed by ticker. The snapshot-dict design is *better* (atomic frame, fewer events) and `planning/archive/MARKET_INTERFACE.md:232-249` already documents it — so **fix the doc, not the code**. Two project docs currently describe two wire formats.

### H2 — No "daily change %" baseline exists
`backend/app/market/models.py:18-28`.

`PriceUpdate.change_percent` is tick-to-tick (~500ms), a sub-cent number. PLAN.md §10 requires a *daily* change % in the watchlist, but nothing stores a session-open/reference price. This will block the frontend. Fix: capture an `open_price`/`reference_price` per ticker at `start()` and `add_ticker`, and expose a daily-change field in `to_dict()`.

### H3 — Cross-thread data race in Massive poller
`backend/app/market/massive_client.py:97, 123-128, 66-76`.

`_fetch_snapshots` reads `self._tickers` inside an `asyncio.to_thread` worker while `add_ticker`/`remove_ticker` mutate it on the event loop. `.append()` during a concurrent read is not guaranteed safe — a classic Heisenbug. Fix: snapshot `tickers = list(self._tickers)` on the event loop before crossing the thread boundary. (Simulator is immune — `step()` is fully synchronous.)

---

## Medium

- **M1** — `PriceCache.version` read outside the lock (`cache.py:64-67`); GIL-safe today, breaks under free-threaded Python. Prior review flagged it; not fixed. The SSE loop polls this as its sole change signal.
- **M2** — Massive `add_ticker` (`massive_client.py:66-70`) doesn't seed the cache, so a new ticker is invisible for up to `poll_interval` (15s); the simulator seeds immediately (`simulator.py:242-249`). Inconsistent across the "one interface." Fix: trigger an immediate poll after add.
- **M3** — SSE sends no keep-alive comments (`stream.py:69-85`); with Massive's 15s poll or quiet markets the stream can sit silent and proxies may idle-drop it. Add a `: keepalive\n\n` heartbeat per loop iteration.
- **M4** — `event_probability` is dead/duplicated state on `SimulatorDataSource` (`simulator.py:215, 221-223`); only forwarded once into `GBMSimulator`.

---

## Low / Nits

- **L1** — `stream.py:17,26` module-level `router` makes `create_stream_router` non-idempotent (double-registration in tests). Instantiate the router inside the factory.
- **L2** — Disconnect only checked once per 500ms interval — acceptable latency, documented.
- **L3** — Unknown-ticker seed price is random `[50, 300]` and not persisted (`simulator.py:151`); re-adding yields a different price. Fine for a sim; worth a doc note.
- **L4** — `_poll_once` (`massive_client.py:118-121`) has no backoff on repeated failures; a 429 keeps hammering at the same cadence.
- **L5** — `change`/`direction` semantics differ between sources (500ms window for sim vs 15s for Massive) despite identical field names. Document the intended reference.
- **Nits** — falsy-zero timestamp replaced in `cache.py:30` (use `is not None`); `to_dict()` recomputes derived fields each call; unused `event_loop_policy` fixture in `conftest.py:6-11`; tests reach into private state (`sim._tickers`, `sim._cholesky`, `source._task`).

---

## PLAN.md doc inconsistencies

1. **SSE payload shape (H1):** §6 describes per-event fields; code + `archive/MARKET_INTERFACE.md` use a snapshot dict. Update §6 to describe the `data: {"AAPL": {...}, ...}` frame and the `retry` directive.
2. **Daily change % (H2):** §10 requires "daily change %" but §6 / `PriceUpdate` define no daily baseline. Specify whether daily change is computed backend-side (stored open price) or accumulated frontend-side from SSE.

---

## What's done well

- `models.py` — `frozen=True, slots=True` immutable dataclass with derived properties; 100% tested.
- `simulator.py:98-101` — GBM discretization is mathematically correct (`exp((mu - 0.5σ²)dt + σ√dt·Z)`), guaranteeing positive prices.
- Cholesky-correlated sector moves (`simulator.py:154-172`) — correct approach, nice realism.
- Both background loops catch-and-continue and are cleanly cancellable/idempotent on `stop()`.
- `simulator.py:152` uses `dict(DEFAULT_PARAMS)` to avoid shared-mutable-dict aliasing — subtle bug correctly avoided.
- SSE version-based change detection avoids redundant payloads; nginx buffering disabled proactively (`stream.py:44`).
- Cache seeded at `start()` so the frontend has data on the first SSE frame.
