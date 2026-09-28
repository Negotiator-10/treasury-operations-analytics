# Power BI Build Guide: Cash Position and Forecast Ladder

This report answers the first question a treasury operations desk asks every
morning: which fund, in which currency, at which bank, will be short of cash or
holding too much idle cash between today and T+5, and what do we do about it
today.

The engine (Python) and the Excel model have already done the calculations.
Power BI loads the results and presents them. Build it in three pages:
Cash Ladder, Daily Position and Exceptions, Forecast Accuracy.

---

## 1. The daily cycle in plain words

1. **Morning.** Start from yesterday's closing balance on the bank statement.
2. **Ladder.** Add every flow we already know about (subscriptions, redemptions,
   trade settlements, coupons, fees, FX) to project the balance for today and
   the next 5 business days.
3. **Act on today.** If today's projection is below the minimum buffer, fund the
   account from the money market fund (MMF). If it is above the idle threshold,
   sweep the excess into the MMF so it earns interest. Both bring the account
   back to its target balance.
4. **Evening.** Book what actually happened from the bank statement. Compare
   actual with forecast. Anything outside tolerance, any overdraft, any idle
   cash goes to the exceptions register with the flow that caused it.

---

## 2. Files to load

All in `data\`. Load the CSVs (Home, Get Data, Text/CSV). They have every value
already calculated.

| File | Rows | One row per | Rename query to |
|---|---|---|---|
| `dim_account.csv` | 15 | bank account (fund, currency, bank, policy limits) | `Accounts` |
| `daily_position.csv` | 900 | account per day (opening, forecast, action, actual, closing, flags, cost) | `Daily` |
| `cash_ladder.csv` | 5,400 | account per as-of date per horizon T+0 to T+5 | `Ladder` |
| `exceptions.csv` | 35 | exception, with root cause and the flow behind it | `Exceptions` |
| `cash_flows.csv` | 2,714 | individual cash flow (forecast side and actual side) | `Flows` |

Leave out `fx_rates.csv` and `_scenarios.csv` (reference only).

The Excel file `Cash_Position_Forecast_Model.xlsx` was recalculated and saved in
Excel, so its values are stored and Power BI can read it too. The CSVs are still
the cleaner source.

### Data types (Transform Data, check each)

- **Daily:** `Date` Date. `Is*` columns Whole Number. All amount and `USD`
  columns Decimal Number.
- **Ladder:** `AsOfDate`, `ValueDate` Date. `Horizon`, `HasActual` Whole
  Number. Amount and `USD` columns Decimal Number. The actual columns are empty
  for value dates after 2026-08-21 (not happened yet). Power BI treats them as
  blank, which is correct.
- **Exceptions:** `Date` Date. `AmountLocal`, `AmountUSD` Decimal Number.
- **Flows:** the three date columns Date. `ActualValueDate` is empty for failed,
  duplicate and pending flows, which is correct.

Close and Apply.

---

## 3. Model (relationships)

Model view. Power BI may auto-detect these; check them:

- `Accounts[AccountID]` 1 to many `Daily[AccountID]`
- `Accounts[AccountID]` 1 to many `Ladder[AccountID]`
- `Accounts[AccountID]` 1 to many `Exceptions[AccountID]`
- `Accounts[AccountID]` 1 to many `Flows[AccountID]`

All single direction, from Accounts outward. Delete any relationship Power BI
creates between the fact tables themselves (for example Daily to Ladder).

Use `Accounts[FundName]`, `Accounts[Currency]` and `Accounts[Bank]` for
slicers, so a single slicer filters every page.

---

## 4. Measures

Create a blank table called `_Measures` (Home, Enter Data, OK) to keep them
together.

```DAX
-- balances ------------------------------------------------------------
Cash Balance USD =
VAR d = MAX ( Daily[Date] )
RETURN CALCULATE ( SUM ( Daily[ClosingUSD] ), Daily[Date] = d )

MMF Balance USD =
VAR d = MAX ( Daily[Date] )
RETURN CALCULATE ( SUM ( Daily[MMFBalanceUSD] ), Daily[Date] = d )

-- treasury actions ----------------------------------------------------
Fundings = CALCULATE ( COUNTROWS ( Daily ), Daily[ActionType] = "FUND FROM MMF" )
Sweeps   = CALCULATE ( COUNTROWS ( Daily ), Daily[ActionType] = "SWEEP TO MMF" )

-- exceptions ----------------------------------------------------------
Exceptions Raised = COUNTROWS ( Exceptions )
High Severity = CALCULATE ( COUNTROWS ( Exceptions ), Exceptions[Severity] = "High" )
Overdrafts = SUM ( Daily[IsOverdraft] )
Idle Cash Days = SUM ( Daily[IsIdle] )
Variance Breaches = SUM ( Daily[IsVarianceBreach] )

-- cost of getting it wrong --------------------------------------------
Overdraft Cost USD = SUM ( Daily[OverdraftCostUSD] )
Idle Cash Drag USD = SUM ( Daily[IdleDragUSD] )

-- ladder (use with an As-of date slicer) ------------------------------
Funding Needs = CALCULATE ( COUNTROWS ( Ladder ), Ladder[LadderFlag] = "FUNDING NEED" )
-- ladder balances are cumulative, so take each account's WORST day in the
-- 5-day window, then add accounts (summing horizons would double count)
Funding Need USD =
SUMX ( VALUES ( Ladder[AccountID] ), CALCULATE ( MAX ( Ladder[FundingNeedUSD] ) ) )
Surplus To Sweep USD =
SUMX ( VALUES ( Ladder[AccountID] ), CALCULATE ( MAX ( Ladder[SurplusUSD] ) ) )
Actions Today =
CALCULATE ( COUNTROWS ( Ladder ), Ladder[Horizon] = 0, Ladder[TreasuryAction] <> 0 )
Projected Balance USD = SUM ( Ladder[ProjClosingUSD] )

Ladder Colour =
SWITCH (
    SELECTEDVALUE ( Ladder[LadderFlag] ),
    "FUNDING NEED", "#F8CBAD",
    "SURPLUS TO SWEEP", "#FFE699",
    "ACTION: FUND FROM MMF", "#C6EFCE",
    "ACTION: SWEEP TO MMF", "#C6EFCE",
    BLANK ()
)

-- forecast accuracy ---------------------------------------------------
Forecast Accuracy =
1 - DIVIDE ( SUM ( Ladder[AbsErrorUSD] ), SUM ( Ladder[ActualGrossUSD] ) )
```

Format: all `USD` measures as currency with no decimals. `Forecast Accuracy` as
percentage with one decimal.

**What Forecast Accuracy means:** of all the cash that actually moved, what
share did the forecast get right. 100% means every flow was forecast with the
right amount on the right day. It is weighted by value, so a USD 40m fail
counts more than a USD 5k bank charge.

---

## 5. Page 1: Cash Ladder (the morning view)

Theme colour navy `#1F3864`. Keep red for problems only.

**Slicers (top):**
- `Ladder[AsOfDate]`. Style Dropdown, Single select on. Select **2026-08-21**
  (the latest day).
- `Accounts[FundName]` and `Accounts[Currency]`. Dropdown, multi select.

**Cards (one row of four):**

| Card | Measure |
|---|---|
| Accounts needing action today | Actions Today |
| Funding needs, next 5 days | Funding Needs |
| Funding required USD | Funding Need USD |
| Surplus to sweep USD | Surplus To Sweep USD |

**The ladder matrix (the main visual, full width):**
- Rows: `Accounts[FundName]`, then `Accounts[AccountID]` (expand all down one level).
  Add `Accounts[Currency]` if you want it visible.
- Columns: `Ladder[HorizonLabel]` (T+0 to T+5).
- Values: `Projected Balance USD`.
- Conditional formatting, Background colour, Format style **Field value**, based
  on the measure `Ladder Colour`. Red cells need funding, yellow cells have
  surplus to sweep, green cells are where treasury acted today.

**Bar chart (bottom right):** "Largest funding needs". Y axis `Accounts[AccountID]`,
X axis `Funding Need USD`, sorted descending, Top N 5.

**Try it:** set As-of to **2026-08-03** and look at account A05 (US Treasury Bond
Fund). A USD 36m redemption was notified 5 days ahead, so the ladder shows the
funding need on 2026-08-10 every day from 2026-08-03. On 2026-08-10 treasury
funds it and the account closes positive. That is the control working
(scenario S7).

---

## 6. Page 2: Daily Position and Exceptions

**Slicers:** `Daily[Date]` (Between style), `Accounts[FundName]`, `Accounts[Bank]`.

**Cards:**

| Card | Measure | Value over the full period |
|---|---|---|
| Exceptions raised | Exceptions Raised | 35 |
| High severity | High Severity | 22 |
| Overdraft cost | Overdraft Cost USD | about USD 6.7k |
| Idle cash drag | Idle Cash Drag USD | about USD 130k |

**Line chart:** X axis `Daily[Date]` (switch it from Date Hierarchy to plain
Date), Y axis `SUM(Daily[ClosingUSD])`. Total operating cash across all accounts.

**Stacked column chart:** X axis `Exceptions[Date]`, Y axis `Exceptions Raised`,
Legend `Exceptions[ExceptionType]`.

**Table (the exceptions register):** `Exceptions[Date]`, `AccountID`, `FundName`,
`ExceptionType`, `Severity`, `AmountUSD`, `RootCause`, `RootFlowID`, `Source`,
`SuggestedAction`. Conditional formatting: `Severity` red background when "High".

**Drill to the flow (optional, shows depth):** add a table of `Flows` with
`FlowID`, `FlowType`, `Counterparty`, `KnownDate`, `ForecastValueDate`,
`ForecastAmount`, `ActualValueDate`, `ActualAmount`, `Status`, `Note`. Click
an exception, then type its `RootFlowID` into a slicer on `Flows[FlowID]`, or
create a relationship `Exceptions[RootFlowID]` to `Flows[FlowID]` (many to one,
single direction) so clicking an exception filters the flow table.

---

## 7. Page 3: Forecast Accuracy

**Column chart:** X axis `Ladder[HorizonLabel]`, Y axis `Forecast Accuracy`.
Expected shape:

| Horizon | Accuracy |
|---|---|
| T+0 | 96.4% |
| T+1 | 96.2% |
| T+2 | 73.2% |
| T+3 to T+5 | about 44.5% |

**Line chart:** X axis `Ladder[AsOfDate]`, Y axis `Forecast Accuracy`, with a
visual-level filter `Ladder[Horizon]` = 0. Dips show the days with fails or
unforecast flows.

**Bar chart:** Y axis `Exceptions[RootCause]`, X axis `Exceptions Raised`. Tells
you where to fix the process (settlement fails vs late notices vs data quality).

**Matrix:** rows `Accounts[FundName]`, columns `Ladder[HorizonLabel]`, values
`Forecast Accuracy`. Shows which fund's forecast is least reliable.

---

## 8. What the numbers should be (tie-out)

If your report shows these, it is built correctly:

- 60 business days, 2026-06-01 to 2026-08-21, 6 funds, 15 accounts
- 35 exceptions: 27 forecast variance, 5 actual overdraft, 2 idle cash, 1 duplicate flow
- 12 of them come from the 6 injected test scenarios, 23 are natural (13 settlement
  fails, 10 late or unforecast flows)
- Treasury actions: 138 fundings from MMF, 94 sweeps to MMF, 668 days no action
- Cash balance on 2026-08-21: about USD 81.9m
- Forecast accuracy T+0: 96.4%

---

## 9. The test scenarios

Each one was injected on purpose and the verification checks prove the engine catches it.

| # | Date | Account | What happened | What the engine raised |
|---|---|---|---|---|
| S1 | 2026-07-01 | A10 UK Equity Income, GBP | Large redemption notified after the morning forecast | Variance + overdraft |
| S2 | 2026-07-14 | A01 Global Equity Index, USD | USD 40m sale failed to settle, arrived 2 days late | Variance + overdraft, root cause settlement fail |
| S3 | 2026-07-08 | A07 Euro Corporate Bond, EUR | Coupon paid net of 30% withholding tax, forecast was gross | Variance, root cause amount variance |
| S4 | 2026-07-23 then 07-27 | A12 Japan Equity, JPY | Subscription loaded twice from the transfer agent file | Duplicate flagged 2 days early; not fixed, so treasury swept out cash that never came: overdraft |
| S5 | 2026-07-20 | A05 US Treasury Bond, USD | Large subscription received after the sweep cut-off | Variance + idle cash |
| S6 | 2026-08-05 | A14 India Equity, INR | Unforecast margin call from the clearing house | Variance + overdraft |
| S7 | 2026-08-10 | A05 US Treasury Bond, USD | Large redemption notified 5 days ahead | Nothing. Ladder warned for 5 days, treasury funded it in time |
