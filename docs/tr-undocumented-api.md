# Trade Republic API — richer endpoints than libtrsync / trdump currently use

Research notes from the decompiled Android app (`traderepublic_java`, app version
`13.40.5`). Every claim below is grounded in a decompiled class — file paths are
given so they can be re-checked when TR ships a new build.

The headline: several figures that libtrsync reconstructs from the timeline, and
that `trdump` either computes client-side or declares out of scope, are served
**directly and pre-computed** by dedicated REST services on
`https://api.traderepublic.com`. These are plain authenticated HTTPS calls
(same `tr_session` cookie + `x-aws-waf-token` header as the login flow) — *not*
the `sub`/`unsub` WebSocket that both projects are built around.

Use `scripts/probe_rest.py` to confirm any of these against a live session
before depending on them.

---

## What we know today (baseline)

**libtrsync** reconstructs everything from the WebSocket timeline
(`timelineTransactions` → `timelineDetailV2`) and the dual-legged mapper. Realized
P&L, dividends received, and cost basis are *derivable* from that history but the
library does no aggregation — it hands back parsed legs.

**trdump** (`../trdump`) live-subscribes to `compactPortfolioByTypeV2`,
`portfolioStatus`, `ticker`, `compactPortfolio`, `availableCash`. Its `monitor`
shows an **unrealized** snapshot only, and its README explicitly lists as out of
scope: lifetime/realized P&L, dividend-adjusted cost basis, XIRR, TWR. Its
`fx.py` pulls EUR reference rates from the **ECB** and states *"TR doesn't stream
one (confirmed: no FX topic)"* — **this is now wrong** (see §3).

The endpoints below close most of those gaps.

---

## 1. Realized P&L + dividend return — `GET /api/v2/taxes/pnl`  ✅ verified live

The single biggest gap-closer. Server-computed realized P&L **and** dividend
return, each as an absolute amount *and* a percentage. No timeline replay needed.

> **Live-probed 2026-05-29.** Works for stocks / ETFs / funds. The real
> response **echoes `instrumentId`** (not in the decompiled model). It returned
> **empty (404)** for crypto (`XF…`), bonds (`US58013MFT62`, `US91282CMM00`)
> and fixed-savings (`IE0000VITHT2`) — so this endpoint is **equities/funds
> only**; crypto/bonds/savings P&L still needs the timeline.
>
> The `absolute` amounts are solid and match the app. Samples:
> TSMC ADR (held) `realizedPnL 0 / dividendReturn 4.81 EUR`; a fully-sold US
> stock `realizedPnL 83.00 EUR`; a fully-sold NL stock `realizedPnL 302.00 /
> dividendReturn 10.80 EUR`.
>
> **`relative` was absent in *every* probed response — even on the €83 and €302
> realised gains.** So in practice (FR locale, this build) the percentage is
> not populated; rely on `absolute` only and compute the percentage yourself
> from cost basis if you need it.

**Endpoint** (`defpackage/yte0.java`):

```
GET /api/v2/taxes/pnl
      ?secAccNo={secAccNo}        # repeatable — pass one per securities account
      &instrumentId={isin}        # required, single instrument (ISIN)
→ List<ReturnPnlApiModel>          # one entry per secAccNo
```

**Response shape** (`de/traderepublic/instrument/components/widgets/data/api/model/ReturnPnlApiModel.java`,
`ReturnPnlDataApiModel.java`):

```jsonc
// real response (fully-sold NL stock) — instrumentId echoed; note NO "relative" despite a €302 realised gain
[
  {
    "secAccNo": "0254693503",
    "instrumentId": "NL0010273215",
    "realizedPnL":   { "absolute": { "value": "302.00000000", "currency": "EUR" } },
    "dividendReturn":{ "absolute": { "value":  "10.80000000", "currency": "EUR" } },
    "lastUpdatedTimestamp": "2026-05-05T08:54:22.808790Z"
  }
]
// The decompiled model (ReturnPnlDataApiModel) carries an optional "relative" BigDecimal,
// but it was never populated in any live probe — including realised gains. Treat it as absent.
```

- `relative` is a fraction (BigDecimal, e.g. `0.0812` = +8.12 %).
- `realizedPnL` / `dividendReturn` are **nullable** (null until the position has
  a realised event / a dividend).
- The mapper (`defpackage/nf60.java`, `ReturnPnlMapper`) copies these straight
  through — the app does **zero** realized-P&L math itself.

**Scope — per instrument, not portfolio-wide.** The caller
(`defpackage/t7m.java`) invokes it as `(List<PortfolioAccount> portfolios,
String instrumentId)`: one instrument, broken down across the user's securities
accounts. A **portfolio-level** realized P&L / dividend total is obtained by
summing the per-instrument calls over the held set (or via the tax-calculation
endpoints in §4). There is no single "whole-portfolio realized P&L" topic.

**It's a lookup, not an enumeration — you cannot discover sold ISINs from it.**
`instrumentId` is *required* (verified: omitting it → `MISSING_REQUIRED_PARAMETER`).
The endpoint cannot list "everything with realized P&L". And a **fully-sold**
position is gone from `compactPortfolioByTypeV2` too, so neither the portfolio
nor the taxes APIs can surface it. The app sidesteps this by only ever calling
`taxes/pnl` from an instrument detail page (ISIN already in hand from search /
watchlist / a tapped timeline event). To compute realized P&L across
*everything you've ever sold*, the workflow is therefore two-stage:
1. **discover** the ISIN set from the **timeline** (SELL events carry the
   instrument — exactly what libtrsync parses), then
2. **look up** each ISIN via `taxes/pnl` for TR's own netted figure.
There is no closed/sold-positions REST endpoint (the only `closed` list,
`/api/v2/timeline/inbox/closed`, is the timeline inbox, not positions).

**No positions endpoint enumerates sold instruments — checked exhaustively.**
Every *positions* list is current-holdings-only: `compactPortfolioByTypeV2`
(WS) and `GET /api/v1/customer-reach/transfer/accounts/{secAccNo}/positions`
(transfer-out eligible) both omit anything you no longer hold. The `CLOSED` /
`INACTIVE` statuses in the app are *sub-portfolio* states (crypto / fixed-income
/ private-markets sleeve open-or-closed), **not** sold-instrument markers. The
only data containing sold instruments is the **transaction ledger**, in two
equivalent forms — both transaction streams, not positions lists:
- **WS `timelineTransactions`** — the primary source (what libtrsync parses).
- **REST CSV export** — `POST /api/v1/portfolio-analytics/transactions/export/request`
  body `{ "from": <date>, "to": <date> }` → `{ jobId, status, estimatedSecondsRemaining }`;
  poll `GET /api/v1/portfolio-analytics/transactions/export/status?jobId=…` until
  done, then download. Async, date-bounded, CSV. (Not live-probed — it spawns a
  server-side export job.)

**Why it beats what we have:** trdump calls dividend-adjusted basis and realized
P&L out of scope; libtrsync would have to sum `SELL`/`DIVIDEND`/`FEE`/`TAX` legs
and match lots itself. This endpoint returns TR's own figure — the one the user
sees in-app — already netted and as a percentage.

---

## 2. Lot-level cost basis — `GET /api/v2/taxes/positions`  ❌ never returned data

Per-lot acquisition detail: the building blocks behind realized P&L and the
dividend-adjusted basis trdump wants.

> **Live-probed 2026-05-29 — could not get a populated response.** The endpoint
> is real (omitting `pageSize` returns a clean `MISSING_REQUIRED_PARAMETER`, so
> the path/params below are correct), but with valid params it returned **404
> in every case tried**:
> - currently-held, never-sold positions (TSMC, ETFs, …);
> - **fully-sold** positions *with* a realised gain (`USN070592100` realized
>   €83; `NL0010273215` realized €302).
>
> So the earlier "lots appear after a sale" guess is **disproven**. The trigger
> condition is unknown — plausibly it only returns open sub-positions for a
> **currently-held, partially-sold** instrument (0 open lots → 404 otherwise),
> or it's gated/not enabled for FR accounts like income-analytics (§4). If you
> want lot data, the timeline (`relatedTransactions` per event) remains the only
> confirmed source on this account.

**Endpoint** (`defpackage/yte0.java`):

```
GET /api/v2/taxes/positions
      ?secAccNo={secAccNo}        # single securities account
      &instrumentId={isin}
      &pageNumber={n}
      &pageSize={n}
→ SubPositionsResponseApiModel
```

**Response** (`SubPositionsResponseApiModel.java`, `PageApiModel.java`,
`LotApiModel.java`, `RelatedTransactionApiModel.java`):

```jsonc
{
  "page":  { "size": 20, "totalElements": 7, "totalPages": 1, "number": 0 },
  "lots": [
    {
      "lotId": "<id>",
      "size":  "10",                 // shares in this lot (BigDecimal)
      "cost":  "1234.50",            // acquisition cost (BigDecimal)
      "currency": "EUR",
      "relatedTransactions": [ /* RelatedTransactionApiModel: links back to timeline events */ ],
      "executionTimestamp": "2024-03-01T09:00:00Z"
    }
  ],
  "lastUpdatedTimestamp": "2026-05-29T10:00:00Z"
}
```

**Why it beats what we have:** this is the authoritative per-lot cost basis with
`relatedTransactions` back-references into the timeline — exactly what you need
for FIFO/average matching, dividend-adjusted basis, or reconciling libtrsync's
parsed legs against TR's own lot accounting. Paginated, so cheap to page.

---

## 3. FX rates ARE on the WebSocket — `ticker` on synthetic LSX instruments  ✅ verified live

trdump's `fx.py` says TR streams no FX rate and falls back to the ECB daily feed.
The app proves otherwise: it gets EUR conversion rates from the **same `ticker`
topic** trdump already uses, against synthetic currency instruments listed on
exchange **`LSX`**.

**Source** (`defpackage/jj40.java` `subscribeToConversionRateFromEUR`, the
`QuotesTopic.Ticker` path; rate math in `defpackage/egl.java`):

| Foreign currency | Instrument ID | Ticker sub `id` |
|---|---|---|
| USD | `LS000IUSD006` | `LS000IUSD006.LSX` |
| GBP | `LS000IGBP005` | `LS000IGBP005.LSX` |
| CHF | `LS000ICHF002` | `LS000ICHF002.LSX` |
| JPY | `LS000IJPY001` | `LS000IJPY001.LSX` |

```
sub <id> {"type":"ticker","id":"LS000IUSD006.LSX"}
```

Rate used by the app = **mid of bid/ask**:
`(bid.price + ask.price) / 2`, 6 dp, `HALF_EVEN` (falls back to `bid` if `ask`
is absent) — `egl.getAvgConversionRate()`.

**Direction — verified.** Live probe of `LS000IUSD006.LSX` returned
`bid 1.163 / ask 1.164` → mid **≈ 1.1635**. That is **units of foreign currency
per 1 EUR** (USD per 1 EUR), i.e. classic `EUR/USD` — the **same convention as
trdump's ECB feed**, *not* the reciprocal I'd guessed from the code. So it's a
drop-in: TR's LSX mid plugs straight into trdump's existing
`value = netSize × price/100 ÷ EURUSD` with no inversion.

**Why it matters for trdump:** that bond-valuation path can use TR's own
intraday LSX mid instead of a once-daily ECB reference — same source TR uses,
same orientation, no external HTTP, no day-stale rate. Only USD/GBP/CHF/JPY are
wired up in this build; other currencies still need a fallback. (`LSX` ticker
requires WS protocol `34`, same as `compactPortfolioByTypeV2`.)

---

## 4. Dividend / income analytics — `/api/v1/income-analytics/...`  ⚠️ gated (Unauthorized)

Dedicated dividend service, per instrument per securities account. Two calls:

> **Live-probed 2026-05-29.** `events-screen` returned
> `{"errorCode":"UNAUTHORIZED"}` on the **same** session that `taxes/pnl`
> accepts — so the income-analytics service is gated behind something extra
> (separate scope/token audience, or a feature flag / market not enabled for
> this account). Treat as **not generally available**; `taxes/pnl.dividendReturn`
> (§1) is the reliable source for received dividends.

**4a. Dividend event history (UI-shaped)** (`defpackage/mzo.java`,
`IncomeEventsApiModel.java`):

```
GET /api/v1/income-analytics/incomes/{secAccNo}/instrument/{instrumentId}/events-screen
      ?size={BigDecimal}
→ IncomeEventsApiModel   # { title, tabs[]: { title, type, items[]: { imageId, title, subtitle, value } } }
```

Already shaped for display (tabs/items with formatted `value` strings) rather
than raw amounts — useful as a cross-check, less so as a data source.

**4b. Income/dividend projection (calculator)** (`IncomeReturnsApiModel.java`,
`IncomeReturnsRequestApiModel.java`):

```
POST /api/v1/income-analytics/incomes/{secAccNo}/instruments/{instrumentId}/returns
  body: { "amount": {money}, "size": {shares} }     # IncomeReturnsRequestApiModel (also carries totalPrice)
→ { "current": {money}, "expected": {money} }        # IncomeReturnsApiModel
```

Projects current vs expected income for a hypothetical position size — a
forward-looking estimate, not booked history.

**Portfolio-level dividends:** for *received* dividend totals prefer
`taxes/pnl.dividendReturn` (§1) summed across holdings; this income-analytics
service is per-instrument and partly projection/estimate oriented.

**Related (not yet inspected in depth):**
- `GET /api/v1/customer-reach/corporate-actions/upcoming` — upcoming dividends / corporate actions.
- `PUT /api/v1/customer-reach/dividend-option/instructions/{instructionId}` — dividend reinvestment election.

---

## 5. Other tax / income endpoints worth knowing

Full inventory from the decompiled Retrofit interfaces (`grep '@GET("/api...taxes'`):

| Endpoint | Likely use |
|---|---|
| `GET /api/v2/taxes/calculations` | Tax calculations (realized gains/loss tax view) |
| `GET /api/v2/taxes/calculations/export/{status,download}`, `POST .../export/request` | Annual tax-report export (async job) |
| `GET /api/v2/taxes/information`, `/api/v1/taxes/information` | Tax settings / status |
| `GET /api/v1/taxes/exemptionorders`, `PUT .../exemptionorders` | Freistellungsauftrag (DE tax exemption order) |
| `GET /api/v1/banking/consumer/interest/payouts`, `/payouts/{id}` | Cash-interest payout history |
| `GET /api/v2/interest-experience/interest/{cashAccountNumber}/home` | Interest dashboard |
| `GET /api/v1/fixed-income/bonus/transactions/{secAccNo}` | Saveback/bonus transactions |

**Live-probed 2026-05-29:**
- `GET /api/v2/taxes/calculations` → `{"items": [], "cursors": {}}` — a
  **cursor-paginated list** (of tax-calculation records), empty on the test
  account. It is *not* a portfolio realized-P&L aggregator; don't expect a
  single total here.
- `GET /api/v2/taxes/information` → the locale tax-status panel (e.g. FR:
  `"Dispense" / "Non éligible"`, dialog `INELIGIBLE_FOR_FRANCE_PFU`).

So there is **no single whole-portfolio realized-P&L endpoint** in this build —
the per-instrument `taxes/pnl` (§1), summed across holdings, remains the path to
a portfolio total.

---

## 6. WebSocket topic versions — `ticker` → `tickerV2`  ✅ verified live

Audit of the topics trdump (`monitor`/`fetch`) and libtrsync currently send vs
the app's current build: **`tickerV2` is the only version bump available.**
Everything else they use is already current — `compactPortfolioByTypeV2` and
`timelineDetailV2` are the V2s; `timelineTransactions`, `accountPairs`,
`instrument`, `homeInstrumentExchange`, `availableCash`, `neonSearch`,
`privateMarketsPositions` have no newer variant.

`QuotesTopic.Ticker` (`de/traderepublic/quotes/impl/data/model/QuotesTopic.java`)
now defaults its `type` to `tickerV2`; libtrsync (`client.py`) and trdump both
still send the legacy `ticker`.

**Different request shape** — split fields, not the combined `id`:

```
legacy:   sub <id> {"type":"ticker",   "id":"IE00B5BMR087.LSX"}
tickerV2: sub <id> {"type":"tickerV2", "isin":"IE00B5BMR087","exchangeId":"LSX","unit":"EUR"}
```

`unit` is the quote-currency code and is **required** (a wrong/missing `unit`
→ `JSON_PARSE_ERROR … validation failed`).

**Different (flatter) payload** — live-probed on a real ETF:

| legacy `ticker` | `tickerV2` |
|---|---|
| `bid/ask/last/pre/open`, each `{time,price,size}` | flat `bidPrice/askPrice/bidSize/askSize/prePrice/openPrice` + one top-level `time` |
| `qualityId` (`"realtime"`), `leverage`, `delta` | **dropped** |
| — | **`unit` echoed** (quote currency) |

```jsonc
// tickerV2, IE00B5BMR087.LSX
{ "time": 1780047850465, "bidPrice": "700.24", "askPrice": "700.32",
  "bidSize": "215", "askSize": "215", "prePrice": "698.7",
  "openPrice": "699.36", "unit": "EUR" }
```

**Verdict — lateral modernization, not a strict upgrade.** Gains: `unit` echo
(quote currency with no separate lookup — directly useful for the §3 FX / bond
currency work) and explicit bid/ask sizes. Loses: `last` (on the LS exchange
`last.price == bidPrice`, so map **Last → `bidPrice`**, **Day Δ → `prePrice`**),
plus `qualityId` (real-time-vs-delayed flag) and `leverage`/`delta` (leveraged
/ derivative products only — not surfaced by trdump's monitor). Migration is
clean for trdump's needs; legacy `ticker` otherwise carries strictly more, so
there's no urgency.

## Auth & parameters (for probing or implementing)

- **Base:** `https://api.traderepublic.com` (`TR_API_BASE` in libtrsync).
- **Auth:** the `tr_session` cookie (on the request's cookie jar) +
  `x-aws-waf-token` / `x-tr-device-info` headers — exactly libtrsync's
  `TRClient._headers()` + its `requests.Session` jar. The `tr_session` JWT lives
  ~5 min; refresh via `TRClient.refresh_session()` as usual.
- **`secAccNo`** comes from the `accountPairs` WS sub
  (`securitiesAccountNumber`) — libtrsync `fetch_account_pairs()`, or
  `scripts/probe_rest.py --account N` auto-fills it.
- **`instrumentId`** is the bare ISIN (e.g. `US0378331005`). Note this differs
  from the WS `ticker` `id`, which is `ISIN.exchange` (e.g. `US0378331005.NSY`).

## Verification status (live-probed 2026-05-29)

| Finding | Evidence | Result |
|---|---|---|
| `taxes/pnl` shape & semantics | `yte0`, `ReturnPnlApiModel`, `nf60` | ✅ works; equities/funds only; echoes `instrumentId`; `relative` never populated (even on realised gains) |
| `taxes/positions` lots | `yte0`, `LotApiModel`, `SubPositionsResponseApiModel` | ❌ 404 in all cases — incl. fully-sold positions with realised gains; trigger unknown |
| FX via LSX `ticker` ids | `jj40`, `egl`, `woe` | ✅ `LS000IUSD006.LSX` mid ≈ 1.1635 = **USD per 1 EUR** (same as ECB; drop-in) |
| income-analytics calls | `mzo`, `IncomeEventsApiModel`, `IncomeReturnsApiModel` | ❌ `UNAUTHORIZED` — gated/not enabled |
| `taxes/calculations` portfolio total? | grep | ❌ empty cursor-list, not an aggregator |
| `ticker` → `tickerV2` | `QuotesTopic` | ✅ only version bump in use; flatter, echoes `unit`, drops `last`/`qualityId`/`leverage`/`delta` |

Decompiled class names (`yte0`, `nf60`, …) are obfuscated and **will change**
between app builds; the API paths and JSON field names are the stable contract.
