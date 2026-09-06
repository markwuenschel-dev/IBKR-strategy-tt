# IBKR Tastytrade engine — V4

A single-process, paper-first options engine. The entire architecture is one line:

```
SCAN → TASTYTRADE → REVIEW → TRADE → RECORD → NEXT SYMBOL
```

There is no scheduler, no controller process, no worker, no claims, leases, gate
files, heartbeats, receipts, or recovery latches. One process runs one loop.

## The loop

`ibkr_trader/runner.py` is the whole system, and `Runner._process_symbol` fits on
a screen:

```python
for symbol in universe:
    snapshot  = market_data.snapshot(symbol)      # scan
    decision  = tastytrade.evaluate(...)          # pure algorithm
    if isinstance(decision, NoTrade):
        record(NO_TRADE); continue
    review    = reviewer.review(decision)         # exactly one, only if a trade exists
    if not review.approved:
        record(REVIEW_REJECTED); continue
    execution = broker.submit(decision)           # submit
    record(execution)                             # durable
```

Every symbol is independent. An ordinary failure on one symbol produces a
recorded outcome for that symbol and nothing else; the next symbol is evaluated
regardless. No symbol-local failure becomes a day-wide mode.

## Layout

| Module | Responsibility |
|---|---|
| `runner.py` | The loop. Owns orchestration, and nothing else does. |
| `tastytrade.py` | **Pure** trade qualification: IV, DTE, delta, liquidity, credit, sizing. |
| `scanner.py` | IBKR market data and option chains → `MarketSnapshot`. |
| `reviewer.py` | One bounded LLM review per proposed trade. |
| `broker.py` | Combo (`BAG`) order construction and submission. |
| `store.py` | SQLite record: attempts, proposals, reviews, orders, fills. |
| `config.py` | The one runtime configuration model. |
| `ports.py` | The four Protocol seams the runner depends on. |
| `models.py` | Frozen domain values. |
| `clock.py` | Time as an injected dependency. |

`tastytrade.py` is the functional core: no I/O, no clock, no mutable state.
Everything effectful lives behind a Protocol in `ports.py`, which is what lets
the whole pipeline be tested without a network.

## Running

```bash
cp trader.example.toml trader.toml     # then edit
python -m ibkr_trader run               # one pass over the universe
python -m ibkr_trader loop              # repeat until the close
```

Requires TWS or IB Gateway on the configured paper port, `ib_async`
(`pip install -e ".[broker]"`), and `ANTHROPIC_API_KEY` for the reviewer.

Exit codes: `0` success, `2` invalid configuration, `3` cannot reach IBKR.

## Configuration

One model, validated completely at startup, before anything connects. Invalid
settings name the field, the value supplied, and the constraint violated, then
exit non-zero having placed no orders and started nothing:

```
Invalid configuration in trader.toml:
  field:      ibkr.refresh_limit
  supplied:   300
  constraint: Input should be less than or equal to 200
  limit:      le=200
```

Cross-field contradictions are caught too — a `target_dte` outside its own
`min_dte`/`max_dte` band, or a missing `ibkr.account`.

## Which account this trades

`ibkr.account` is required, and it is checked against the session. After
connecting, the process asks TWS which accounts it manages and refuses to start
unless the configured one is among them, closing the session first.

**What that proves, exactly:** that the process reached the account you named.
It does *not* prove that account is a paper account. IBKR exposes no paper/live
indicator anywhere in the connection handshake, the `DU` prefix everyone relies
on is a convention IBKR has never documented, and this repository's own verified
paper account — `DUR318607` — does not match the shape people usually assume.
**Paper safety is you naming the paper account.**

A paper run pointed at a conventionally live port (7496/4001) now *warns* rather
than refusing. IBKR documents those as defaults that "can be changed to any open
socket port", so a live TWS on 7497 would have passed the old check while an SSH
tunnel or a container port-map would have been blocked by it. The port is a hint
about intent; the account is evidence about the session.

**The `paper` flag enforces nothing, and this is deliberate to state plainly.**
It has exactly two readers in the whole engine — its own declaration and the
port warning above — so a `paper = true` run that names a live account will
connect to that account and trade it. There is nothing for the flag to check
against: IBKR exposes no paper/live indicator, so the process cannot tell which
kind of session it opened. The flag is a declaration of intent with no runtime
effect today; the account check is the only enforcement in this area, and it
enforces *identity*, not mode. Giving the declaration its first real reader — a
run-level record of the mode, the verified account and the endpoint — is the
next change in this sequence, and it is a prerequisite for enabling live
operation. `tests/test_account_identity.py` pins the current state, so that
change cannot arrive silently.

## Testing

```bash
python -m pytest
```

`tests/test_mission.py::test_production_runner_places_known_good_order` is the
mission test. It drives the real runner end to end and asserts that exactly one
expected order — `3x 185/180 put credit spread @ 1.75` — reaches the broker and
is durably recorded. It must never be made green by bypassing the runner, and it
cannot skip on a clock condition: time comes from an injected `FixedClock`, so it
behaves identically at any hour.

Only the four external boundaries are faked. The algorithm, the runner, and the
SQLite store are the production implementations in every test.

## Repeat scans and resting orders

`loop` runs a pass, sleeps `scan_interval_seconds` (default 300), and repeats
while the market is open. The real cadence is *pass duration + interval*, not
the interval alone — a live single-symbol pass measured 69s, most of it option
chain qualification.

Because passes repeat, an order that has not filled yet must not be proposed
again. IBKR's position stream reports only **filled** holdings, so `portfolio()`
additionally reports still-working orders as `Position(pending=True)`, and the
algorithm counts them as exposure:

```
pass 1   SPY   WORKING    IBKR status PreSubmitted
pass 2   SPY   NO_TRADE   a working order in SPY is already outstanding
```

Without this, a limit order resting for half an hour at the default interval
would be submitted six times. `max_positions` counts pending exposure too, so a
working order occupies a concentration slot. Set
`risk.allow_duplicate_symbol = true` to opt out.

## Recording

Five tables: `symbol_attempts`, `trade_proposals`, `reviews`, `orders`, `fills`.

This is a record of history, not runtime state. Nothing is read back to decide
what the runner does next; losing the file would cost the audit trail, not the
ability to operate. One `proposal_id` names the trade in the proposal row, the
review, the broker's `orderRef`, and the fill.

## Verified against live IBKR paper

Confirmed on paper account `DUR318607` (TWS v178):

- `portfolio()` reads real account state; `snapshot()` returns real chains
  (437 SPY quotes, real deltas, 0.4-0.7% spreads) and a real IV rank.
- **A full pass placed a real paper order.** One `run_once()` over `["SPY"]`
  scanned 388 live quotes, proposed a `753/748` put credit spread at `1.04`
  credit (short leg delta **-0.2999** against a 0.30 target), had it reviewed and
  approved, submitted it, and recorded `WORKING / PreSubmitted` with
  `broker_order_id 1639896659`. `orderRef` matched `proposal_id` exactly, so the
  identity chain held end to end. The order was cancelled afterwards; the pass
  took 69s. Market was closed, so it queued rather than filling — which is why
  the outcome is `WORKING` and not `FILLED`.
- **A resting order blocks a duplicate.** Two consecutive live passes with the
  first order still unfilled: pass 1 `WORKING`, pass 2
  `NO_TRADE — a working order in SPY is already outstanding`, one order total.
- **The combo encoding is correct.** `whatIfOrder` on a 5-wide SPY put vertical
  reports `initMarginChange = 500.00` — exactly `width x 100`, IBKR's margin for
  a *defined-risk short vertical*. A naked short put would demand roughly
  $10-15k; an inverted (debit) spread would show near-zero margin and a negative
  equity change. Equity change was **+60.66**, i.e. a credit received.
  `tests/test_broker_encoding.py` pins the payload that produced this.

## Known limitations

- **Open interest is unavailable under frozen/delayed market data.** IBKR
  delivers it only on live data. Running with `market_data_type` 2, 3 or 4 while
  `min_open_interest > 0` screens out every contract, and every symbol reports
  `NO_TRADE` on liquidity. Set `min_open_interest = 0` for off-hours dry runs.
- **`is_market_open` knows no holidays.** It checks weekday and session hours
  only. On a holiday it reports open, finds no quotes, and records data errors
  rather than trading.
- **IV rank falls back to realized volatility** when IBKR's implied-volatility
  series is unavailable. The fallback is logged at WARNING; see
  `IBKRMarketData._iv_rank` for its documented limitations.
