# IBKR Tastytrade engine — V4

A single-process, paper-first options engine. The entire architecture is one line:

```
MANAGE (what is held) → SCAN + TASTYTRADE (every symbol) → RANK → REVIEW → TRADE → RECORD (best first, until the slots are full)
```

There is no scheduler, no controller process, no worker, no claims, leases, gate
files, heartbeats, receipts, or recovery latches. One process runs one pass.

## The pass

`ibkr_trader/runner.py` is the whole system. A pass has three phases after
[managing what is already held](#management), and the whole thing fits on a
screen:

```python
manager.run()                                     # 0. work the open spreads first

candidates = []
for symbol in universe:                           # 1. scan and evaluate everyone
    snapshot  = market_data.snapshot(symbol)
    decision  = tastytrade.evaluate(...)          # pure algorithm
    if isinstance(decision, NoTrade):
        record(NO_TRADE); continue                # recorded now, not later
    candidates.append(decision)                   # a proposal is only a candidate

ranked = ranking.rank_proposals(candidates)       # 2. pure; best first

free = max_positions - open - pending
for candidate in ranked:                          # 3. best first, until the book is full
    if free == 0:
        record(NOT_SELECTED, rank); continue      # no review is spent on it
    decision  = tastytrade.evaluate(re-quote)     # the quote has aged; evaluate again
    review    = reviewer.review(decision)         # exactly one, only for a candidate
    if not review.approved:
        record(REVIEW_REJECTED); continue
    execution = broker.submit(decision)
    record(execution); free -= 1                  # durable
```

**Phase 1** scans and evaluates every symbol in the universe. `NO_TRADE`,
`DATA_ERROR` and `AWAITING_DECISION` are recorded the moment they happen. A
proposal is not traded yet; it is held as a candidate.

**Phase 2** ranks the candidates. The score is a composite of percentile ranks —
credit/width 50%, IV rank 30%, leg tightness 20% — where each percentile is
computed against the pass's own field: 1.0 is the best proposal *today*, 0.0
the worst. A lone candidate scores 1.0.

**Phase 3** takes the candidates in rank order. Each one is re-quoted and
re-evaluated (a quote decays in minutes; the ranking was built on the first
one), reviewed exactly once, and submitted, until the free position slots —
`max_positions` minus open and pending — are used. Whatever is left is recorded
`NOT_SELECTED` with its rank, and no review is spent on it.

Why bother: with 102 names and 10 slots, universe order fills the book with the
first ten names alphabetically that clear the screens, not the ten best setups.
Ranking is how the strategy's stated preference — better credit for the width,
richer IV, tighter legs — becomes the decision.

Every symbol is independent for failures: an ordinary failure on one symbol
produces a recorded outcome for that symbol and nothing else, and the next is
evaluated regardless. No symbol-local failure becomes a day-wide mode. Symbols
are *not* independent for submission — that is ordered by rank, and one
candidate taking a slot is exactly what keeps a worse one out.

## The trade

One structure, the put credit spread, selected the Tastytrade way:

- Trade only when IV rank is at least 30 and an expiry lies 30-60 days out,
  45 preferred.
- **Sell** the put nearest 0.30 delta. **Buy** the put nearest 0.20 delta in
  the same expiry. The width is whatever the distance between those two strikes
  turns out to be — an output of selection, not a setting.
- The credit must be at least one third of that width, or there is no trade. A
  spread that cannot pay is rejected, never resized.
- Size at the smallest of three ceilings: the 2% per-trade risk budget on net
  liquidation, available buying power, and the 10-contract cap. Zero contracts
  is a `NO_TRADE` that names which ceiling bound.

Within one pass, buying power is also reduced by what higher-ranked candidates
in the same pass already sent to the venue, so the sixth candidate is not sized
against cash the first committed. The risk budget is not reduced — it is a fraction
of account value, not of free cash — and nothing carries into the next pass.

## Management

Opening a spread is half the strategy. `ibkr_trader/manager.py` is the other
half, and it runs as phase A of every pass — before any symbol is quoted — and
on its own under `python -m ibkr_trader manage`. It reads the account's option
positions and the venue's working orders once, then works every spread the
engine opened:

- **Reconcile fills.** An opening order that reached the venue but had not
  filled when its pass ended (`WORKING`) is matched against the held legs. Short
  put short, long put long → a `spreads` row at the fill price (the limit when
  no fill was reported), sized to the smaller leg. Not held → left alone.
- **Profit target, from day one.** While the spread has more than `manage_dte`
  days to expiration, a GTC buy-back rests at `profit_target_ratio` of the
  credit, rounded *up* to the tick: `1.75` collected → buy back at `0.88`. If
  the venue no longer shows that order while the legs are still held, it is
  placed again. When the legs are gone and the order is gone, the target
  filled and the spread is `CLOSED`.
- **The 21-DTE rule.** At `manage_dte` (default 21) the spread stops being
  held. The profit order is cancelled and the legs are quoted; the debit to
  close is `short mid − long mid`, rounded up. Then, if `management.roll` is
  on, the scan's own selection is run for the symbol one cycle out — with this
  symbol's positions hidden from the duplicate guard, since the roll replaces
  them. The result is a **roll** only when it nets a credit (new credit >
  close debit), is no wider and no larger than what is held, and the reviewer
  approves it. A roll is a `ROLL_CLOSE` (DAY, at the debit) followed, once
  that fills, by a `ROLL_OPEN` (DAY, the new spread), recorded as an opening
  order like any other. Anything else — no net credit, sizing to zero, review
  rejected or failed, rolling disabled — is recorded as `ROLL_DECLINED` and the
  spread is simply closed with a DAY order at the debit.
- **A DAY order that lapses** (`CLOSING`/`ROLLING`, legs still held, reference
  no longer working) is placed again on the next pass, roll attempt included.

Two records mean "a human must look":

- `NEEDS_DECISION` — the engine declined to act alone: a leg with no usable
  market to close on, a spread quoting at no net debit, or legs held unevenly
  (one assigned or exercised). The spread's status becomes `NEEDS_DECISION`
  and no pass touches it again until someone changes that.
- `UNMANAGED_POSITION` — an option leg the account holds that belongs to no
  spread the engine opened (entered by hand, or left by a partial close).
  Reported once per symbol per pass; never traded.

Every step is one row in `management_actions`, and the spread's lifecycle
(`OPEN → PROFIT_ORDER_RESTING → CLOSING | ROLLING → CLOSED`) is one row in
`spreads`, keyed by the opening proposal's id. Spreads are independent for
failures exactly as symbols are: an error on one is recorded as `ERROR` for
that spread and the next is processed. A failure before any spread could be
reached — the position stream unreadable — is one `ERROR` row for the run,
and the scan still runs.

One invariant is enforced in code, not by convention: **no management order
ever adds contracts or widens a spread.** `manager.added_risk` is the named
check, applied at the single chokepoint every order passes through and again
before a roll is even reviewed.

## Layout

| Module | Responsibility |
|---|---|
| `runner.py` | The loop. Owns orchestration, and nothing else does. |
| `manager.py` | Phase A: reconciles fills, rests the profit target, rolls or closes at `manage_dte`. Never adds risk. |
| `tastytrade.py` | **Pure** trade qualification: IV, DTE, delta, liquidity, credit, sizing. |
| `ranking.py` | **Pure.** Orders the pass's proposals: credit/width, IV rank, leg tightness. |
| `scanner.py` | IBKR market data and option chains → `MarketSnapshot`. |
| `reviewer.py` | One bounded LLM review per proposed trade. Two backends behind one port: the Claude Code CLI on the operator's subscription (default) and the Anthropic API. |
| `broker.py` | Combo (`BAG`) order construction, submission, cancellation, and the working-order view. |
| `store.py` | SQLite record: attempts, proposals, reviews, orders, fills, spreads, management actions. |
| `config.py` | The one runtime configuration model. |
| `ports.py` | The four Protocol seams the runner depends on. |
| `models.py` | Frozen domain values. |
| `clock.py` | Time as an injected dependency. |

`tastytrade.py` and `ranking.py` are the functional core: no I/O, no clock, no
mutable state.
Everything effectful lives behind a Protocol in `ports.py`, which is what lets
the whole pipeline be tested without a network.

## Running

```bash
cp trader.example.toml trader.toml     # then edit
python -m ibkr_trader run               # manage the book, then one pass over the universe
python -m ibkr_trader loop              # repeat until the close
python -m ibkr_trader manage            # work the open spreads only; scan nothing
```

Requires TWS or IB Gateway on the configured paper port and `ib_async`
(`pip install -e ".[broker]"`). The reviewer runs through the Claude Code CLI on
the operator's subscription login by default: `claude` must be on PATH and
logged in (`claude /login`), and no API key is needed. The direct API backend
is opt-in — set `backend = "anthropic_api"` under `[reviewer]` and export
`ANTHROPIC_API_KEY`.

The universe is whatever `trader.toml` lists. The paper configuration this
repository is run with scans the Nasdaq-100 — 102 names, since Alphabet lists
two share classes — with `scan_interval_seconds = 1800`. A pass over that many
names quotes 8,000-12,000 option lines and takes 10-20 minutes.

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

**The `paper` flag declares intent; it does not prove the venue's mode.** IBKR
exposes no paper/live indicator, so the process cannot independently tell which
kind of session it opened. The account check enforces *identity*, not mode.
Before each pass scans its first symbol, the engine durably records the declared
mode, the account confirmed by the session, and the connection endpoint in the
`runs` table. If that row cannot be written, the pass does not start and no
order can be submitted without account provenance.

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

`loop` runs a pass, sleeps `scan_interval_seconds`, and repeats while the
market is open. The default is 300; the Nasdaq-100 configuration sets 1800,
because the real cadence is *pass duration + interval*, not the interval alone.
A live single-symbol pass measured 69s, most of it option chain qualification,
and a pass over ~100 names takes 10-20 minutes — five minutes of sleep would
start the next pass before the current one had ended.

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

Eight tables: `runs`, `symbol_attempts`, `trade_proposals`, `reviews`, `orders`,
`fills`, `spreads`, `management_actions`.

The first six are a record of history, not mutable coordination state: nothing
in them is read back to decide what the runner does next. The last two are
read back, by the manager alone, and only to know which spreads it is
responsible for and what it last did with each — the venue's positions and
working orders, not these rows, decide what happens next. A new pass requires its
`runs` row to commit before scanning: if that write fails — the file is locked
by another writer for longer than the 5-second busy timeout, or is read-only —
`start_run` raises, the pass never starts, and nothing is submitted. A
*missing* file is not that case. `SqliteStore` creates the parent directory
and opens the path with `sqlite3.connect`, which creates an empty database
(`store.py:114-115`), so deleting the file loses the history and nothing else:
the next run starts a fresh record and trades normally. One `proposal_id` names
the trade in the proposal row, the review, the broker's `orderRef`, and the
fill.

## Verified against live IBKR paper

Confirmed on paper account `DUR318607` (TWS v178). These runs were made when
the long strike was a fixed `spread_width = 5` below the short, so the
`753/748` and "5-wide" figures below are outcomes of that setting. The long put
is now chosen by delta and the width is derived, so a given name will not
generally come out 5 wide; what these runs verified — the order encoding, the
identity chain, the duplicate guard, and the `width x 100` margin arithmetic —
does not depend on how the width was chosen.

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
