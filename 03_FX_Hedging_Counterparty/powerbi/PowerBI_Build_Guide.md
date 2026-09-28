# Power BI Build Guide: FX Share-Class Hedging and Bank Counterparty Exposure

This report answers two treasury questions every day:

1. Are our currency-hedged share classes still properly hedged (between 95% and
   105%), and which ones need a rebalance trade today?
2. How much do we have at risk with each bank, counting both the cash we hold
   there and the value of our FX forwards with it, and is any bank over its limit?

Everything is already calculated in the CSVs. Build it in three pages: Hedge
Monitor, Counterparty Exposure, Rolls and Exceptions.

---

## 1. The ideas in plain words

**Hedged share class.** A USD fund can offer a EUR class. EUR investors do not
want USD/EUR currency risk, so treasury sells USD forward (buys EUR) for roughly
the size of the class. Then a USD move does not hit their EUR return.

**Hedge ratio** = forward notional outstanding / class NAV, both in the fund's
base currency. Target is 100%. The forward notional stays fixed between trades
but the NAV moves every day (market returns, subscriptions, redemptions), so the
ratio drifts.

**The rules used here:**
- Outside **97% to 103%** at the close: rebalance next business day (soft band).
- Outside **95% to 105%**: breach. This is the ESMA guideline for hedged share
  classes (over-hedging must not exceed 105%, under-hedging must not fall below
  95%).
- Forwards are **1-month** and **rolled** on the last business day of each
  month. The roll resets the hedge to 100% and settles the old forwards. That
  settlement is real cash, in or out of the fund.

**Counterparty exposure.** If a bank fails, we lose:
- the **cash** we hold with it (Engine 1's closing balances), plus
- the **positive value** of our forwards with it (what it would owe us today).

Each bank has a **limit** set by credit rating. Above 90% of the limit is a
warning, above 100% is a breach. Separately, no single FX bank may hold more than
**50%** of all forward notional (concentration rule).

---

## 2. Files to load

All in `data\` (Get Data, Text/CSV):

| File | Rows | One row per | Rename to |
|---|---|---|---|
| `dim_share_class.csv` | 11 | hedged share class | `Classes` |
| `dim_counterparty.csv` | 9 | bank, with type, rating and limit | `Counterparties` |
| `hedge_daily.csv` | 660 | class per day (NAV, hedge, ratio, band, flags, MTM) | `Hedge` |
| `counterparty_daily.csv` | 540 | bank per day (cash, MTM, exposure, utilization, share) | `Exposure` |
| `fx_forwards.csv` | 71 | forward contract (initial, rebalance, roll) | `Forwards` |
| `roll_settlements.csv` | 10 | fund per roll date (cash settled on the roll) | `Rolls` |
| `exceptions.csv` | 10 | exception episode (start, end, days, peak) | `Exceptions` |

Load the CSVs, not `FX_Hedging_Counterparty_Model.xlsx`. The workbook's
formulas have not been calculated by Excel yet, so Power BI would read blanks.
(Open the workbook in Excel once and check its **Checks** sheet shows all PASS.
That is your proof the formulas agree with the engine.)

### Data types (Transform Data)

- All `Date` columns (and `TradeDate`, `MaturityDate`, `SettledDate`,
  `StartDate`, `EndDate`) as Date.
- `HedgeRatio`, `Utilization`, `NotionalShare` as Decimal Number, then format
  them as Percentage in the report.
- `Is*` columns and `Days` as Whole Number. Amount columns as Decimal Number.
- `NotionalShare` is empty for custody banks (they hold no forwards). That is
  correct.

---

## 3. Model

- `Classes[ClassID]` 1 to many `Hedge[ClassID]`
- `Classes[ClassID]` 1 to many `Forwards[ClassID]`
- `Counterparties[Counterparty]` 1 to many `Exposure[Counterparty]`

Leave `Rolls` and `Exceptions` unrelated (they are read as registers). Delete any
relationship Power BI creates between `Hedge` and `Exposure`.

---

## 4. Measures

Create a `_Measures` table (Enter Data, OK), then:

```DAX
-- hedging --------------------------------------------------------------
-- use the USD columns: base-currency amounts of different funds (USD, EUR, GBP, JPY)
-- cannot be added together, so a total built from them would be meaningless
Hedge Ratio = DIVIDE ( SUM ( Hedge[HedgeNotionalUSD] ), SUM ( Hedge[ClassNAVUSD] ) )

Hedged NAV USD =
VAR d = MAX ( Hedge[Date] )
RETURN CALCULATE ( SUM ( Hedge[ClassNAVUSD] ), Hedge[Date] = d )

Classes In Band =
VAR d = MAX ( Hedge[Date] )
RETURN CALCULATE ( COUNTROWS ( Hedge ), Hedge[Date] = d, Hedge[BandStatus] = "OK" )

Hard Breach Days = SUM ( Hedge[IsHardBreach] )
Rebalance Trades = SUM ( Hedge[IsRebalanceDay] )
Missed Rebalances = SUM ( Hedge[IsMissedRebalance] )
Forward MTM USD = SUM ( Hedge[MTMUSD] )

Band Low Hard = 0.95
Band Low Soft = 0.97
Band High Soft = 1.03
Band High Hard = 1.05

-- counterparty -----------------------------------------------------------
Exposure USD =
VAR d = MAX ( Exposure[Date] )
RETURN CALCULATE ( SUM ( Exposure[ExposureUSD] ), Exposure[Date] = d )

Limit USD = SUM ( Counterparties[LimitUSD] )

Peak Utilization = MAX ( Exposure[Utilization] )

Limit Breach Days = SUM ( Exposure[IsLimitBreach] )
Concentration Breach Days = SUM ( Exposure[IsConcentrationBreach] )

Utilization Colour =
VAR u = MAX ( Exposure[Utilization] )
RETURN SWITCH ( TRUE (), u > 1, "#F8CBAD", u > 0.9, "#FFE699", BLANK () )

-- rolls and exceptions ------------------------------------------------------
Roll Cash USD = SUM ( Rolls[SettlementUSD] )
Exception Episodes = COUNTROWS ( Exceptions )
Open Exceptions = CALCULATE ( COUNTROWS ( Exceptions ), Exceptions[Status] = "Open" )
```

Format `Hedge Ratio`, the four band measures and `Peak Utilization` as
Percentage (one decimal). USD measures as currency, no decimals.

---

## 5. Page 1: Hedge Monitor

**Slicers:** `Classes[ClassName]` (dropdown, single select for the chart),
`Hedge[Date]` (between).

**Cards:**

| Card | Measure |
|---|---|
| Hedged class NAV (latest) | Hedged NAV USD |
| Classes in band today | Classes In Band (out of 11) |
| Rebalance trades | Rebalance Trades |
| Hard breach days | Hard Breach Days |

**Line chart (the main visual):** X axis `Hedge[Date]` (plain Date, not the
hierarchy), Y axis `Hedge Ratio`, plus the four band measures as extra lines.
Format the band lines dashed and grey (hard) or light grey (soft). Select one
class in the slicer and you can see the ratio drift, touch 97% or 103%, and snap
back to 100% the next day (rebalance) or at month-end (roll).

- Pick **C01** (Global Equity Index Fund EUR Hedged): on 2026-06-23 a 12%
  redemption pushes it to 113.5%. That is scenario H1.
- Pick **C08** (UK Equity Income Fund USD Hedged): in July it drifts under 97%,
  nobody rebalances for three days and it falls to 93.1%. That is scenario H2.

**Matrix:** rows `Classes[ClassName]`, columns `Hedge[BandStatus]`, values
count of `Hedge[Date]`. Shows which classes spend the most days out of band.

---

## 6. Page 2: Counterparty Exposure

**Slicer:** `Exposure[Date]` (single date, default 2026-08-21), and
`Counterparties[CounterpartyType]`.

**Clustered bar chart:** Y axis `Counterparties[Counterparty]`, X axis
`Exposure USD` and `Limit USD` side by side. Anything where the exposure bar
passes the limit bar is a breach.

**Matrix (heatmap):** rows `Counterparties[Counterparty]`, columns
`Exposure[Date]`, values `Peak Utilization`. Conditional formatting,
background colour, Field value, based on `Utilization Colour`. Red cells are
breaches, yellow cells are warnings. Bank C on 2026-07-20 is red (133%). That is
the same idle cash as Engine 1's scenario S5, now seen as credit risk (H3).

**Line chart:** X axis `Exposure[Date]`, Y axis `SUM(Exposure[NotionalShare])`,
Legend `Exposure[Counterparty]`, filtered to FX counterparties. Add a constant
line at 50%. FX Bank 2 jumps to 66% on 2026-07-31 (scenario H4).

**Cards:** Limit Breach Days, Concentration Breach Days.

---

## 7. Page 3: Rolls and Exceptions

**Clustered column chart:** X axis `Rolls[Date]`, Y axis `Roll Cash USD`,
Legend `Rolls[FundID]`. Negative bars are cash the fund must pay on the roll.

**Table:** `Rolls` with `Date`, `FundName`, `SettlementUSD`, `AccountTargetBase`,
`ExceedsCashBuffer`. Highlight `ExceedsCashBuffer` = 1: the UK Equity Income Fund
paid USD 6.0m on the June roll, more than its operating account's cash buffer.
That cash call has to be pre-funded in the Engine 1 cash ladder.

**Table (exceptions register):** `ExceptionID`, `ExceptionType`, `Entity`,
`StartDate`, `EndDate`, `Days`, `Severity`, `Peak`, `Detail`, `Source`,
`Status`, `SuggestedAction`. Colour `Severity` = High red.

---

## 8. Tie-out (your report should show these)

- 11 hedged share classes, 660 class-days, 71 forward contracts (11 initial,
  22 roll, 38 rebalance)
- Hedged class NAV on 2026-08-21: about USD 1.84bn
- Band status over the period: 619 OK, 35 rebalance due, 6 hard breach days
- 10 exception episodes: 5 hedge ratio breaches, 1 missed rebalance, 1 limit
  breach, 1 limit warning, 1 concentration breach, 1 roll cash call
- Peak utilization: Bank C 133%, Bank A 93%, all FX banks under 45%

---

## 9. Test scenarios

| # | Date | Entity | What happened | What the engine raised |
|---|---|---|---|---|
| H1 | 2026-06-23 | C01 EUR hedged class | 12% of the class redeemed in one day, hedge left at 113.5% | Hedge ratio breach, fixed by next-day rebalance |
| H2 | 2026-07-20 to 07-22 | C08 USD hedged class | Rebalances not booked for 3 days (desk short-staffed) | Missed rebalance, then hedge ratio breach at 93.1% |
| H3 | 2026-07-20 | Bank C | Engine 1's late USD 42m subscription left idle at Bank C | Counterparty limit breach at 133% |
| H4 | 2026-07-31 onward | FX Bank 2 | July roll moved FX Bank 1's classes to FX Bank 2 for a better quote | Concentration breach, 66% of notional with one bank |

The engine also found five things nobody injected:
- **Three hedge ratio breaches.** Two (C02, C06) came from large one-day class
  flows and one (C11, Japan Equity) from a big market move. The soft band cannot
  stop a single day's jump.
- **A roll cash call for the UK fund.**
- **A limit warning on Bank A.** This is Engine 1's settlement-fail idle cash on
  2026-08-06.
