# tao-sentinel

A **Bittensor watchtower** built on the [Taostats API](https://docs.taostats.io). One small,
self-hostable tool that keeps an eye on the things that move while you sleep:

- **Alerts** — nine watch types covering alpha-price moves, stake changes, validator
  deregistrations, emission shifts, the TAO/USD price, subnet market-cap moves,
  registration-cost drops, brand-new subnets, and 24h price trends — delivered via the
  console, Telegram, or a generic webhook.
- **Alert digests + cooldown** — Telegram alerts are batched into one severity-grouped digest
  per tick; a configurable per-alert cooldown suppresses repeat noise (unless severity
  escalates). Webhooks stay one-POST-per-alert for machine consumers.
- **dTAO portfolio** — value any coldkey's alpha stake across subnets in TAO and USD, with
  per-position share-of-portfolio percentages.
- **Subnet health** — score and grade subnets (A–F) on validator-stake concentration,
  emission share, neuron counts, and market presence.
- **Dashboard** — a clean, dependency-free dark web page summarizing health, your portfolio,
  and recent alerts, with auto-refresh. The main table sorts, filters, and links each subnet
  to a per-subnet detail page; a pinned **watchlist** shows hand-rolled SVG sparklines for the
  subnets you care about plus the TAO/USD price.

Everything runs **without an API key** in `--mock` mode, so you can try the whole tool
offline against deterministic fixtures before you sign up for anything.

**Live demo: [tao.insightfulbytes.com](https://tao.insightfulbytes.com)** — real mainnet
data, refreshed against the Taostats free tier.

![tao-sentinel dashboard — live mainnet subnet health grades](docs/dashboard.png)

> Open-source reputation project. Issues and PRs welcome.

---

## Screenshots

> _Placeholder — add screenshots here._
>
> - `docs/screenshot-dashboard.png` — the web dashboard
> - `docs/screenshot-scan.png` — `tao-sentinel scan` rich table
> - `docs/screenshot-portfolio.png` — `tao-sentinel portfolio` output

---

## Install

Requires **Python 3.10+**.

```bash
# from a clone of this repo
pip install -e .

# with dev/test extras (pytest)
pip install -e ".[dev]"
```

This installs the `tao-sentinel` command. Confirm it works in mock mode (no key needed):

```bash
tao-sentinel scan --mock
```

---

## Quickstart

`tao-sentinel` has a global `--mock` switch on the relevant commands. In mock mode every
command works end-to-end against built-in deterministic fixtures (subnets `apex`, `targon`,
`chutes`, etc., a sample coldkey with three positions, and a TAO price of $350) — **no network
and no API key required**. Drop `--mock` and supply a key to hit the live Taostats API.

### `init` — write an example config

```bash
tao-sentinel init
```

Writes a commented `./sentinel.yaml` you can edit (see [Configuration](#configuration)).

### `scan` — subnet health scanner

```bash
# scan all subnets (mock demo)
tao-sentinel scan --mock

# scan a single subnet by netuid (pulls validators for a deeper score)
tao-sentinel scan 1 --mock

# machine-readable output
tao-sentinel scan --mock --json
```

Prints a rich table of subnets with a 0–100 health score and a color-coded grade
(`A` ≥ 85, `B` ≥ 70, `C` ≥ 55, `D` ≥ 40, else `F`), plus human-readable warnings.

### `portfolio` — value a coldkey's dTAO stake

```bash
# the mock fixture coldkey (holds three positions) -- copy-paste verbatim
tao-sentinel portfolio 5MockColdkey0000000000000000000000000000000000000000000 --mock
tao-sentinel portfolio <COLDKEY_SS58> --json
```

Reports the TAO (and USD, via the current TAO/USD price) value of each stake position. Each
position's `value_tao` is resolved by this precedence:

1. **The API-provided value** (`balance_as_tao`) when present — this is the authoritative
   current valuation from Taostats and is used as-is.
2. Otherwise, `alpha_staked * pool.price_tao` when the position's subnet has a pool entry.
3. Otherwise `None` (the position has no derivable value and is omitted from the total).

`total_value_tao` sums every position with a non-`None` value. Because the API value is
preferred, **root / netuid-0 positions are included** even though root has no dTAO pool entry:
they are valued from the API's `balance_as_tao` rather than dropped, so the total reflects your
full stake instead of understating it.

### `watch` — run the alert engine

```bash
# single pass: take a snapshot, evaluate rules vs. last saved state, dispatch alerts
tao-sentinel watch --config sentinel.yaml --once --mock

# run forever, polling on the configured interval and dispatching to notifiers
tao-sentinel watch --config sentinel.yaml --mock
```

Both modes deliver alerts to **all configured notifiers** (console, Telegram, webhook) —
`--once` is a single tick of the same engine, which also makes it the way to test your
Telegram/webhook setup end to end (and makes cron-driven `--once` deployments work). Pass
`--no-notify` for a console-only dry run that skips outbound notifications.

The engine is **rate-frugal**: it fetches pools once (shared by `price_change`, `market_cap`,
and `new_subnet`), subnets once (shared by `emission_shift` and `registration_cost`), stakes
only for watched coldkeys, validators only for watched netuids, the TAO/USD price once per tick
only if a `tao_price` watch is present, and per-netuid 24h history for `price_trend` watches
through a **6-hour TTL cache** (so it costs one call per watched netuid per 6h, not per tick).
State is persisted between runs at `state_path` (default `~/.tao-sentinel/state.json`) so
changes are measured against the last snapshot.

#### Alert digests and cooldown

Before dispatch, the engine applies a **cooldown dedup**: an alert is suppressed if an
identical one (same rule type + netuid + coldkey + hotkey) already fired within
`alert_cooldown_minutes` — **unless its severity escalated** (`info` < `warning` < `critical`),
which always sends through. Cooldown timestamps persist in the state file, so dedup survives
across `--once` ticks and restarts. Set `alert_cooldown_minutes: 0` to disable dedup entirely.

The surviving alerts are then handed to each notifier as a batch:

- **Telegram** sends **one combined, severity-grouped digest message** per tick (capped at
  ~3500 chars, truncating overflow with an `...and N more` marker) instead of one message per
  alert.
- **Webhook** delivers **one POST per alert** — its per-alert JSON shape is unchanged from
  v0.1.0, so existing machine consumers keep working without modification.
- **Console** prints each alert.

### `serve` — the web dashboard

```bash
tao-sentinel serve --config sentinel.yaml --port 8787 --mock
```

Then open <http://localhost:8787>.

**Frontend stack:** the dashboard is a React SPA — Vite + TypeScript + Tailwind CSS,
TanStack Query/Table, TradingView's `lightweight-charts`, self-hosted Inter (no CDNs) —
served by the same FastAPI process from `tao_sentinel/web/static` (built by `npm run build`
in `frontend/`; shipped inside the wheel and the Docker image, so end users never need
node). Source checkouts without the build fall back to a minimal server-rendered page.

```bash
# frontend development: hot-reload SPA proxied onto a mock API
tao-sentinel serve --mock --port 8787 &
cd frontend && npm install && npm run dev
```

Routes:

- `GET /` — dark single-page dashboard. A pinned **watchlist** section (the netuids in your
  config's `watchlist`) sits above the main table, each with an inline-SVG sparkline of its
  trailing alpha price; the header carries a TAO/USD price sparkline. The **main table** is
  interactive: click a column header to sort (numeric-aware, toggles asc/desc), type to filter
  by name/netuid, toggle grade chips (A B C D F), and each row links to its subnet detail page.
  Below it: a portfolio card (with per-position share bars) if a coldkey is configured in your
  watches, a severity-colored recent-alerts timeline from the state file, and a data-freshness
  footer. Auto-refreshes every 300s.
- `GET /api/status` — the dashboard's data as JSON (subnets, portfolio, alerts, and a `meta`
  block with `generated_at`, `tao_price_usd`, and `tao_price_spark`). Sparkline series are
  included only for watchlist netuids.
- `GET /subnet/{netuid}` — a per-subnet **detail page** (HTML): an authoritative single-subnet
  scan with validator-stake concentration surfaced as explicit risk warnings, pool detail
  (price, market cap, TAO/alpha reserves), a 24h sparkline with its percent change, and the
  top-10 validators by stake with each validator's share percentage. Unknown netuids return a
  404 page.
- `GET /api/subnet/{netuid}` — the same single-subnet detail as JSON; unknown netuids return a
  404 JSON body.

> The portfolio section is populated from the **first coldkey** referenced by any watch. To see
> a populated portfolio in `--mock` mode, set that watch's `coldkey` to the fixture coldkey
> `5MockColdkey0000000000000000000000000000000000000000000` in your `sentinel.yaml` (any other
> address has no fixture positions and renders an empty `0.00 τ` / `$0.00` card).

The dashboard puts a 5-minute in-process TTL cache around the main scan/portfolio data to
respect the API rate limit. Sparklines (watchlist subnets and the TAO price) come from a
longer **6-hour** TTL cache because each one costs a history call, and the per-subnet detail
results are cached for **1 hour** behind an LRU cap of **16** entries so arbitrary
`/subnet/{netuid}` traffic can never run away with the budget.

When multiple processes share one API key (the compose stack runs a watcher **and** a
dashboard), they coordinate through a **cross-process token bucket** — a `flock`-guarded
`ratelimit.json` kept next to the state file — so the pair stays at the real 5 calls/min
instead of each running its own bucket and bursting to double that (which draws 429s,
most visibly at deploy time when the startup cache warm and the first watcher tick
coincide). Ad-hoc CLI runs on the same machine join the same bucket automatically.

---

## Configuration

`tao-sentinel init` writes a commented `sentinel.yaml`. Full reference:

```yaml
# How to authenticate to the Taostats API.
#   - omit / leave null to run in mock mode (or pass --mock)
#   - put the raw key here, OR
#   - use "env:VARNAME" to read it from an environment variable, OR
#   - leave unset and export TAOSTATS_API_KEY (honored as a fallback)
api_key: "env:TAOSTATS_API_KEY"

# Optional Telegram notifier.
telegram:
  bot_token: "123456:ABC-DEF..."
  chat_id: "-1001234567890"

# Optional generic webhook notifier (alert JSON is POSTed here).
webhook_url: "https://example.com/hooks/tao-sentinel"

# How often run_forever polls, in seconds.
poll_interval_seconds: 3600

# Where alert engine state (last snapshot + cooldown timestamps) is stored.
state_path: "~/.tao-sentinel/state.json"

# Suppress an identical alert (same rule type + subnet + coldkey + hotkey)
# fired within this many minutes, unless its severity escalated. 0 disables.
alert_cooldown_minutes: 60

# Subnets to pin at the top of the dashboard, each with a sparkline.
# Capped at 12; each pinned subnet costs one 6h-cached history call.
watchlist: [1, 64]

# What to watch. Each entry is one rule.
watches:
  # price_change: pool price_tao moves beyond threshold_pct.
  - type: price_change
    netuid: 1
    threshold_pct: 10.0

  # stake_change: a coldkey's position alpha_staked moves beyond
  # threshold_pct (or a position appears / disappears). In --mock mode use the
  # fixture coldkey below to get a populated portfolio in `portfolio`/`serve`;
  # replace it with your own ss58 for live mode.
  - type: stake_change
    coldkey: "5MockColdkey0000000000000000000000000000000000000000000"
    threshold_pct: 10.0

  # validator_dereg: a hotkey that was present+active on a netuid
  # goes missing/inactive (severity: critical).
  - type: validator_dereg
    netuid: 1
    hotkey: "5Validator..."

  # emission_shift: a subnet's emission_pct moves beyond threshold_pct (relative).
  - type: emission_shift
    netuid: 4
    threshold_pct: 10.0

  # tao_price: the TAO/USD spot price moves beyond threshold_pct (no netuid).
  - type: tao_price
    threshold_pct: 5.0

  # market_cap: a subnet pool's market cap (TAO) moves beyond threshold_pct.
  - type: market_cap
    netuid: 1
    threshold_pct: 15.0

  # registration_cost: a subnet's registration cost DROPS by threshold_pct.
  - type: registration_cost
    netuid: 4
    threshold_pct: 20.0

  # new_subnet: a brand-new subnet appears in the pool list (no netuid).
  - type: new_subnet

  # price_trend: |change| over the trailing 24h history >= threshold_pct
  # (requires netuid; uses the 6h-cached history endpoint).
  - type: price_trend
    netuid: 64
    threshold_pct: 25.0
```

### Config fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `api_key` | string or null | none | Raw key, `env:VARNAME` indirection (resolved at load), or null. `TAOSTATS_API_KEY` env var is honored as a fallback. |
| `telegram.bot_token` | string | — | Telegram bot token for `TelegramNotifier`. |
| `telegram.chat_id` | string | — | Target chat ID. |
| `webhook_url` | string or null | none | Generic webhook; alert JSON is POSTed here. |
| `poll_interval_seconds` | int | `3600` | Sleep between polls in `run_forever`. A 1-hour tick keeps the engine within the free-tier monthly budget (see [Rate limits](#rate-limits-free-tier)); shorter intervals multiply monthly call volume proportionally. |
| `state_path` | string | `~/.tao-sentinel/state.json` | Where the last snapshot and cooldown timestamps are persisted. |
| `alert_cooldown_minutes` | int | `60` | Suppress an identical alert (same rule type + netuid + coldkey + hotkey) that fired within this window, unless its severity escalated. `0` disables dedup. |
| `watchlist` | list of int | `[]` | Subnets pinned at the top of the dashboard, each with a sparkline. Capped at **12**; must be unique netuids. Each pinned subnet costs one 6h-cached history call. |
| `watches` | list | `[]` | One entry per rule. |

### Watch fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `type` | string | — | One of the nine watch types in the table below. |
| `netuid` | int or null | none | Subnet to watch. Required for `price_trend`; optional for the other per-subnet rules; ignored by `tao_price`/`new_subnet`. |
| `coldkey` | string or null | none | Coldkey ss58 (stake rules). |
| `hotkey` | string or null | none | Hotkey ss58 (validator rules). |
| `threshold_pct` | float | `10.0` | Percent-move threshold that triggers the rule. |

### Watch types

Severities marked "→ critical at 2×" escalate from `warning` to `critical` when the move is at
least twice `threshold_pct`. The "extra API cost" column is the per-tick HTTP cost *beyond* the
shared `pools`/`subnets` fetches the engine already makes (each of those is paginated; see
[Rate limits](#rate-limits-free-tier)).

| Type | Fires when… | Needs `netuid` | Severity | Extra API cost |
|---|---|---|---|---|
| `price_change` | a subnet pool's alpha `price_tao` moves beyond `threshold_pct` between ticks | optional | warning → critical at 2× | reuses `pools` (0) |
| `stake_change` | a watched coldkey position's `alpha_staked` moves beyond `threshold_pct`, or a position appears/disappears | no (uses `coldkey`) | info/warning/critical by move | 1 stake source per watched coldkey |
| `validator_dereg` | a watched hotkey that was present+active on a netuid goes missing/inactive | yes (+`hotkey`) | critical | 1 validators source per watched netuid |
| `emission_shift` | a subnet's `emission_pct` moves beyond `threshold_pct` (relative) | optional | warning | reuses `subnets` (0) |
| `tao_price` | the TAO/USD spot price moves beyond `threshold_pct` | no | warning → critical at 2× | +1 call/tick when any `tao_price` watch is present |
| `market_cap` | a subnet pool's `market_cap_tao` moves beyond `threshold_pct` | optional | warning → critical at 2× | reuses `pools` (0) |
| `registration_cost` | a subnet's `registration_cost_tao` **drops** by at least `threshold_pct` (a cheap-registration sniper) | optional | warning | reuses `subnets` (0) |
| `new_subnet` | a netuid appears in the pool set that was absent last tick (never fires on first run) | no | info | reuses `pools` (0) |
| `price_trend` | the trailing-24h alpha-price change for a netuid is at least `threshold_pct` | yes | warning → critical at 2× | 1 history call per watched netuid per **6h** (6h-cached) |

---

## Getting an API key

Live mode needs a Taostats API key.

1. Sign in at the Taostats Pro dashboard: <https://dash.taostats.io> (an alias for
   <https://taostats.io/pro>).
2. Create an API key under **API Keys**. Keys look like
   `tao-7051ffef-a15f-4608-9fea-1142d61f09a1:92a1cf8a`.
3. Provide it to tao-sentinel via `api_key` in `sentinel.yaml` (raw or `env:VARNAME`) or by
   exporting `TAOSTATS_API_KEY`:

   ```bash
   export TAOSTATS_API_KEY="tao-...:......"
   tao-sentinel scan
   ```

The key is sent as a raw `Authorization` header — **no `Bearer ` prefix** (a common cause of
`401`s).

## Rate limits (free tier)

The Taostats free tier is **5 calls/minute** and (per the project's documented planning
target) on the order of **~10k calls/month**. tao-sentinel is built to live within that:

- A blocking, thread-safe token-bucket `RateLimiter` throttles the HTTP client to the
  configured per-minute limit (5 by default) — you'll never burst past it, even when the web
  dashboard serves concurrent requests off one shared client.
- The alert engine fetches the minimum **sources** needed per poll: pools once (shared by
  `price_change`/`market_cap`/`new_subnet`), subnets once (shared by
  `emission_shift`/`registration_cost`), stakes only for watched coldkeys, validators only for
  watched netuids, and the TAO/USD price once per tick **only** when a `tao_price` watch is
  present. `price_trend` history is fetched per watched netuid through a 6h TTL cache, so it
  costs one call per netuid per 6h — not per tick.
- The subnet scanner only pulls per-validator detail for the single-netuid case; scanning all
  subnets scores from the subnet list alone.
- The web dashboard caches the main scan/portfolio for 5 minutes and shares one pool fetch
  across the health scan and the portfolio view per refill. Watchlist and TAO-price sparklines
  use a longer 6h cache (one history call each), and per-subnet detail pages are cached 1h
  behind an LRU cap of 16 entries.

### Counting calls: list endpoints are paginated

The Taostats list endpoints return **100 rows per page**, so a single logical fetch such as
`get_subnets()`, `get_pools()`, or `get_validators()` is **not one HTTP call** — it is
`ceil(rows / 100)` calls. Bittensor already has well over 100 subnets, so `get_subnets()` and
`get_pools()` are **2 HTTP calls each today** (and become 3 once a list exceeds 200 rows). The
budget math must count pages, not sources:

```
HTTP calls per tick   = sum over each touched source of ceil(rows / 100)
monthly calls         = calls_per_tick * (3600 / poll_interval_seconds) * 24 * 30
                        + 6h-cached history (price_trend + watchlist sparklines + TAO spark)
                        + uncached /subnet/{netuid} detail views (1 each, LRU-capped at 16)
```

Worked example for the watches in the example config. The shared list sources each span two
pages on mainnet today: `pools` (2) + `subnets` (2) + one watched coldkey (1) + one watched
netuid for `validator_dereg` (1) + the TAO price for the `tao_price` watch (1) = **7 HTTP
calls/tick**. The single `price_trend` watch adds a history call only every 6h, not per tick:
1 netuid × (30 × 24 ÷ 6) ≈ **120 calls/month**.

| `poll_interval_seconds` | ticks/hour | calls/tick | per-tick/month | + history | calls/month | verdict |
|---|---|---|---|---|---|---|
| `3600` (default, hourly) | 1 | 7 | 5,040 | 120 | **5,160** | safe (< 10k) |
| `900` | 4 | 7 | 20,160 | 120 | 20,280 | over budget |
| `300` | 12 | 7 | 60,480 | 120 | 60,600 | ~6× over budget |

This is why the default `poll_interval_seconds` is **3600**, not a few minutes: the per-tick
cost is roughly double what a naive "one call per source" estimate would suggest (each list
source is two pages today), and a sub-hourly interval blows past the free-tier monthly cap. The
same per-page reasoning applies when a list grows past the next 100-row boundary — re-check the
budget if your watch set covers many more rows.

The **dashboard** spends separately from the `watch` engine (it is a different process and does
not run on the poll interval): each pinned `watchlist` subnet costs one history call per 6h, the
header TAO-price sparkline one per 6h, and each *uncached* `/subnet/{netuid}` detail view one
call (with at most 16 detail results cached at a time under the LRU cap). With the example
`watchlist: [1, 64]` that is 2 sparkline calls plus the TAO spark per 6h — negligible against
the monthly cap.

Higher limits are available on paid Pro plans at <https://taostats.io/pro>. Note: the exact
monthly cap is not officially published by Taostats; treat the ~10k/month figure as a
conservative planning number.

---

## Files written on disk (permissions)

Two files tao-sentinel writes can hold sensitive data, so both are created with owner-only
`0600` permissions (and their parent directory with `0700`):

- **`sentinel.yaml`** (written by `tao-sentinel init`) — may contain your raw Taostats
  `api_key`, your Telegram `bot_token`, and a `webhook_url`. These are secrets that grant API
  and chat-send access, so the file must not be world- or group-readable on a shared host.
- **The state file** (`state_path`, default `~/.tao-sentinel/state.json`) — holds the watched
  coldkey/hotkey ss58 addresses and the persisted snapshot of portfolio positions. It does not
  contain the API key or bot token, but it does link this host's operator to specific on-chain
  wallets, so it is also written `0600`.

If you provide your own `sentinel.yaml` or relocate the state file, keep these permissions
restrictive yourself.

---

## Endpoint paths are patchable

Taostats occasionally changes or versions its endpoint paths. To make that painless, **all
endpoint paths are centralized in a single `ENDPOINTS` dict in
[`tao_sentinel/api.py`](tao_sentinel/api.py)**. If a path changes, edit that one dict — no need
to touch the rest of the client. Each entry carries a comment noting the confidence of the
path from the API research that informed it.

---

## License

MIT. See [`LICENSE`](LICENSE).

---

## Disclaimer

tao-sentinel is an independent, open-source project and is **not affiliated with or endorsed
by Taostats or the Opentensor Foundation**. It reads public on-chain data via the Taostats
API. Nothing here is financial advice. Verify anything important against the chain and the
official Taostats data before acting on it.
