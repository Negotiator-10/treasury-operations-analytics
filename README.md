# Treasury Operations Analytics

Two self-built tools covering the daily work of a fund treasury operations desk:
where the cash is, what treasury must do about it today, whether currency hedges
are in line, and how much is at risk with each bank.

Each tool has a Python engine, an Excel model with the logic as live formulas, and a
Power BI dashboard. **All data is synthetic** (generated with realistic structure), and
known problems were planted on purpose so the controls can be proven to catch them.

| # | Tool | Question it answers |
|---|---|---|
| 1 | [Cash Position and Forecast Ladder](01_Cash_Position_Forecast) | Which account will be short or hold idle cash between today and T+5, and should treasury fund it from or sweep it to the money market fund today? |
| 3 | [FX Share-Class Hedging and Bank Exposure](03_FX_Hedging_Counterparty) | Are currency-hedged share classes still within 95% to 105%, and is any bank over its credit or concentration limit? |

A third tool, a payments and cut-off control monitor, is in progress.

---

## 1. Cash Position and Forecast Ladder

**Scope:** 6 funds, 15 bank accounts, 5 currencies (USD, EUR, GBP, JPY, INR), 60 business days.

**How it works, each business day**
1. Start from yesterday's closing balance.
2. Project today to T+5 from every flow already known (subscriptions, redemptions,
   trade settlements at T+1 or T+2, coupons, fees, FX). Only flows known that morning
   are used, so the forecast never sees the future.
3. Act on today: fund from the money market fund if below the buffer, sweep to it if
   above the idle threshold.
4. Evening: book the actual flows, compare with the forecast, and raise exceptions,
   each traced to the single cash flow that caused it.

**What it found**
- 35 exceptions: 27 forecast variances, 5 overdrafts, 2 idle cash days, 1 duplicate.
  All 7 planted scenarios caught; the other 23 come from natural settlement fails and late notices.
- A duplicate subscription was flagged two days early but never cleared. Treasury swept out
  cash that never arrived and the account went overdrawn: the control worked, the process did not.
- Forecast accuracy is 96.4% for today but about 44% three days out, and at T+2 the funds
  that settle T+1 (US, India) are 57% accurate against 86% to 89% for T+2 markets.
- Idle cash cost about USD 130k over the period against about USD 6.7k of overdraft interest.

**Validation:** an independent script rebuilds every balance from the raw flows without the
engine code (23 of 23 checks pass, including a no-look-ahead check); the Excel formulas match
the engine on all 900 account-days with zero differences.

---

## 3. FX Share-Class Hedging and Bank Counterparty Exposure

**Scope:** 11 currency-hedged share classes, 71 FX forwards, 9 banks, same 60 days.

**How it works**
- Hedge ratio = forward notional / class NAV. Outside 97% to 103%: rebalance next day.
  Outside 95% to 105% (the ESMA guideline for hedged share classes): breach.
- Forwards are priced from spot and interest rates (interest rate parity) and rolled monthly;
  each roll settles real cash, checked against the fund's cash buffer from tool 1.
- Bank exposure = cash held at the bank (from tool 1) + positive net value of forwards with it,
  against a rating-based limit, plus a 50% cap on any one FX bank's share of the hedges.

**What it found**
- A 12% one-day redemption left a class 113.5% hedged; three missed rebalances let another
  fall to 93.1%.
- Late idle cash from tool 1 put one bank at 133% of its credit limit: idle cash is also credit risk.
- Choosing the best quote at a monthly roll put 66% of all hedges with one bank.
- One fund's monthly roll cost more cash than its account buffer held.
- A USD 10.3m forward loss offset a matching currency gain on the assets: the hedge working as designed.

**Validation:** independent script, 14 of 14 checks pass (re-prices all 71 forwards, re-marks
them daily, rebuilds exposure from tool 1's balances); the Excel model has its own Checks sheet.

---

## What is in each folder

| Path | Contents |
|---|---|
| `*.pbix` | Power BI dashboard (3 pages each) |
| `*_Model.xlsx` | Excel model: the logic as live formulas, interactive sheets, and a Checks sheet |
| `data/` | The CSVs the dashboards load |
| `powerbi/PowerBI_Build_Guide.md` | Data model, DAX measures and page-by-page build |
| `build/` | Python generator, independent verification script and its report |

**Rebuild tool 1 from scratch:** `python build/generate_engine1.py`, then
`python build/verify_engine1.py`, then `python build/build_excel.py` (tool 3 the same, after tool 1).

**Tools:** Python (numpy, openpyxl), Excel (SUMIFS, INDEX/MATCH, named ranges), Power BI (DAX, star schema).

---

Built by Chintamani Deogaonkar | [LinkedIn](https://www.linkedin.com/in/chintamani-deogaonkar-516ab62b2) | [GitHub](https://github.com/Negotiator-10)
