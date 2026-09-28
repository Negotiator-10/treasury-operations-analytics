"""
Engine 1: Daily Cash Position and Forecast Ladder
Treasury Operations Analytics (synthetic data)

What this does, in the order a treasury desk works:
  1. Generates 65 business days of cash flows for 6 funds / 15 bank accounts
     (subscriptions, redemptions, trade settlements, income, fees, FX, misc).
     Every flow has a FORECAST side (what we expected, and when we knew it)
     and an ACTUAL side (what the bank statement shows).
  2. Each morning (as-of date T) it builds a T to T+5 cash ladder per account
     from the flows known by T.
  3. Treasury acts on the T+0 projection: fund from the money market fund if
     projected below the buffer, sweep to the MMF if projected above the idle
     threshold.
  4. End of day it books the actual flows, computes the closing balance, and
     compares actual vs forecast. Breaches go to an exceptions register with
     a root cause traced back to the individual flow.

Known errors are injected (scenarios S1 to S7) so the checks can prove they
catch them. All output is written to ../data as CSV (UTF-8).
"""
import csv
import os
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data")

SEED = 20260927
rng = np.random.default_rng(SEED)

START = np.datetime64("2026-06-01")
N_ACTUAL = 60          # days with actual positions (day 0 .. 59)
N_ALL = 65             # flows needed out to day 64 for the T+5 ladder
MAX_H = 5


def date_of(i):
    return str(np.busday_offset(START, int(i), roll="forward"))


def round_away(x, nd=-3):
    """Excel ROUND semantics (half away from zero), to nd digits (-3 = thousands)."""
    q = 10 ** (-nd)
    return int(np.sign(x) * np.floor(abs(x) / q + 0.5) * q)


# ---------------------------------------------------------------- reference data
CCY = ["USD", "EUR", "GBP", "JPY", "INR"]
FX0 = {"USD": 1.0, "EUR": 1.10, "GBP": 1.30, "JPY": 0.0068, "INR": 0.0115}
FX_VOL = {"USD": 0.0, "EUR": 0.004, "GBP": 0.004, "JPY": 0.005, "INR": 0.002}
# illustrative policy rates, overdraft = policy + 2%, MMF yield = policy - 0.10%
POLICY = {"USD": 3.75, "EUR": 2.00, "GBP": 3.75, "JPY": 0.50, "INR": 5.50}
DAYBASIS = {"USD": 360, "EUR": 360, "GBP": 365, "JPY": 365, "INR": 365}
SETTLE_LAG = {"USD": 1, "INR": 1, "EUR": 2, "GBP": 2, "JPY": 2}

FUNDS = [
    ("F01", "Global Equity Index Fund", "USD"),
    ("F02", "US Treasury Bond Fund", "USD"),
    ("F03", "Euro Corporate Bond Fund", "EUR"),
    ("F04", "UK Equity Income Fund", "GBP"),
    ("F05", "Japan Equity Fund", "JPY"),
    ("F06", "India Equity Fund", "INR"),
]
FUND_NAME = {f[0]: f[1] for f in FUNDS}
FUND_BASE = {f[0]: f[2] for f in FUNDS}

# AccountID, Fund, Ccy, Bank, flow scale in USD millions (typical single flow size)
ACCOUNTS = [
    ("A01", "F01", "USD", "Bank A", 8.0),
    ("A02", "F01", "EUR", "Bank A", 3.0),
    ("A03", "F01", "GBP", "Bank A", 2.0),
    ("A04", "F01", "JPY", "Bank B", 2.0),
    ("A05", "F02", "USD", "Bank C", 6.0),
    ("A06", "F02", "EUR", "Bank C", 1.5),
    ("A07", "F03", "EUR", "Bank D", 4.0),
    ("A08", "F03", "USD", "Bank D", 1.5),
    ("A09", "F03", "GBP", "Bank D", 1.0),
    ("A10", "F04", "GBP", "Bank E", 3.0),
    ("A11", "F04", "USD", "Bank E", 1.0),
    ("A12", "F05", "JPY", "Bank B", 3.0),
    ("A13", "F05", "USD", "Bank B", 1.0),
    ("A14", "F06", "INR", "Bank F", 2.5),
    ("A15", "F06", "USD", "Bank F", 1.0),
]


def nice(x):
    """Round a threshold to 3 significant figures so it looks like a real policy number."""
    if x == 0:
        return 0
    mag = 10 ** (int(np.floor(np.log10(abs(x)))) - 2)
    return int(round(x / mag) * mag)


ACCT = {}
for aid, fid, ccy, bank, scale_usd in ACCOUNTS:
    scale = scale_usd * 1e6 / FX0[ccy]            # flow scale in local currency
    ACCT[aid] = dict(
        AccountID=aid, FundID=fid, FundName=FUND_NAME[fid], FundBaseCcy=FUND_BASE[fid],
        Currency=ccy, Bank=bank,
        AccountType="Operating (base)" if ccy == FUND_BASE[fid] else "Foreign currency",
        scale=scale,
        MinBalance=nice(0.5 * scale),
        TargetBalance=nice(2.0 * scale),
        IdleThreshold=nice(5.0 * scale),
        VarianceFloor=nice(0.25 * scale),
        OverdraftRatePct=POLICY[ccy] + 2.0,
        MMFYieldPct=round(POLICY[ccy] - 0.10, 2),
        DayBasis=DAYBASIS[ccy],
        MMFOpening=nice(30.0 * scale),
    )
BASE_ACCT = {fid: aid for aid, a in ACCT.items() for fid in [a["FundID"]] if a["Currency"] == FUND_BASE[fid]}

# FX path, USD per 1 unit, for day index -20 .. N_ALL-1
FX = {}
for c in CCY:
    path = [FX0[c]]
    for _ in range(N_ALL + 20 - 1):
        path.append(path[-1] * float(np.exp(rng.normal(0, FX_VOL[c]))))
    FX[c] = {i - 20: (round(v, 6) if c != "JPY" else round(v, 8)) for i, v in enumerate(path)}


def fx(c, i):
    return FX[c][max(min(i, N_ALL - 1), -20)]


# ---------------------------------------------------------------- flow book
FLOWS = []
_ref = [0]


def add_flow(acct, ftype, cpty, known_i, fvd_i, famt, avd_i, aamt, status, scenario="Natural", note=""):
    _ref[0] += 1
    FLOWS.append(dict(
        FlowID=f"FL{_ref[0]:05d}", AccountID=acct, FlowType=ftype, Counterparty=cpty,
        known=known_i, fvd=fvd_i, famt=int(famt), avd=avd_i, aamt=int(aamt),
        Status=status, Scenario=scenario, Note=note,
    ))
    return FLOWS[-1]["FlowID"]


def lognorm_amt(scale, med=0.8, sig=0.7):
    return float(scale * med * np.exp(rng.normal(0, sig)))


BROKERS = [f"Broker {i:02d}" for i in range(1, 13)]
FX_BANKS = ["FX Bank 1", "FX Bank 2", "FX Bank 3"]

# 1) Trades on every account: buys (outflow) and sells (inflow), settle T+lag.
#    1% fail and settle 1 to 3 days late (booked as FAILED + REBOOKED rows).
for aid, a in ACCT.items():
    lag = SETTLE_LAG[a["Currency"]]
    for t in range(-lag, N_ALL - lag):
        for _ in range(rng.poisson(1.5)):
            side = 1 if rng.random() < 0.5 else -1
            amt = side * lognorm_amt(a["scale"])
            vd = t + lag
            cp = BROKERS[rng.integers(len(BROKERS))]
            ft = "TRADE_SELL" if side > 0 else "TRADE_BUY"
            if rng.random() < 0.01 and vd + 3 < N_ALL:
                slip = int(rng.integers(1, 4))
                fid = add_flow(aid, ft, cp, t, vd, amt, None, 0, "FAILED", note=f"Settlement fail, rebooked to T+{slip}")
                add_flow(aid, ft, cp, vd + 1, vd + slip, amt, vd + slip, amt, "SETTLED", note=f"Rebooked fail of {fid}")
            else:
                add_flow(aid, ft, cp, t, vd, amt, vd, amt, "SETTLED")

# 2) Subscriptions and redemptions on the fund base account, settle T+2.
#    ~2% arrive as late notices (not in the forecast). Some large redemptions
#    come with advance notice (known 5 days early).
for fid in FUND_NAME:
    aid = BASE_ACCT[fid]
    a = ACCT[aid]
    for t in range(-2, N_ALL - 2):
        for _ in range(rng.poisson(1.2)):
            side = 1 if rng.random() < 0.5 else -1
            amt = side * lognorm_amt(a["scale"], med=0.9, sig=0.6)
            vd = t + 2
            ft = "SUBSCRIPTION" if side > 0 else "REDEMPTION"
            if rng.random() < 0.02:
                add_flow(aid, ft, "Transfer Agent", vd, vd, 0, vd, amt, "UNFORECAST", note="Late dealing notice")
            else:
                known = t - 3 if (side < 0 and rng.random() < 0.10) else t
                add_flow(aid, ft, "Transfer Agent", known, vd, amt, vd, amt, "SETTLED")

# 3) Income (coupons / dividends) on every account, known 10 days ahead.
#    Tiny actual-vs-forecast differences (FX / rounding) are normal.
for aid, a in ACCT.items():
    vd = int(rng.integers(0, 8))
    while vd < N_ALL:
        amt = 0.3 * a["scale"] * float(np.exp(rng.normal(0, 0.4)))
        act = amt * (1 + rng.normal(0, 0.002))
        add_flow(aid, "INCOME", "Paying agent", vd - 10, vd, amt, vd, act, "SETTLED")
        vd += int(rng.integers(6, 12))

# 4) Monthly fees on the first business day of each month, known 10 days ahead.
month_starts = sorted({i for i in range(N_ALL) if i == 0 or date_of(i)[5:7] != date_of(i - 1)[5:7]})
for fid in FUND_NAME:
    aid = BASE_ACCT[fid]
    a = ACCT[aid]
    for vd in month_starts:
        amt = -0.2 * a["scale"] * float(np.exp(rng.normal(0, 0.1)))
        add_flow(aid, "FEE", "Management and custody fees", vd - 10, vd, amt, vd, amt, "SETTLED")

# 5) FX funding trades between a fund's base account and its foreign accounts, T+2.
for fid in FUND_NAME:
    base = BASE_ACCT[fid]
    foreign = [aid for aid, a in ACCT.items() if a["FundID"] == fid and aid != base]
    for t in range(-2, N_ALL - 2):
        if rng.random() < 0.15:
            fa = foreign[rng.integers(len(foreign))]
            fccy, bccy = ACCT[fa]["Currency"], ACCT[base]["Currency"]
            usd = lognorm_amt(ACCT[fa]["scale"] * FX0[fccy], med=0.6, sig=0.5)
            side = 1 if rng.random() < 0.5 else -1        # +1 = buy foreign ccy
            f_amt = side * usd / fx(fccy, t)
            b_amt = -side * usd / fx(bccy, t)
            vd = t + 2
            cp = FX_BANKS[rng.integers(len(FX_BANKS))]
            add_flow(fa, "FX_SETTLEMENT", cp, t, vd, f_amt, vd, f_amt, "SETTLED", note=f"FX {bccy}/{fccy}")
            add_flow(base, "FX_SETTLEMENT", cp, t, vd, b_amt, vd, b_amt, "SETTLED", note=f"FX {bccy}/{fccy}")

# 6) Small unforecast items: bank charges, interest adjustments (not in forecast).
for aid, a in ACCT.items():
    for d in range(N_ALL):
        if rng.random() < 0.30:
            amt = rng.normal(-0.01, 0.03) * a["scale"]
            add_flow(aid, "MISC", "Bank charges / adjustments", d, d, 0, d, amt, "UNFORECAST")

# ---------------------------------------------------------------- injected scenarios
SCEN = {}


def sc(aid):
    return ACCT[aid]["scale"]


# S1: large redemption notice arrives after the forecast (unforecast outflow) -> overdraft
SCEN["S1"] = dict(acct="A10", day=22, expect=["FORECAST_VARIANCE", "ACTUAL_OVERDRAFT"],
                  desc="Late notice of a large redemption, not in the morning forecast")
add_flow("A10", "REDEMPTION", "Transfer Agent", 22, 22, 0, 22, -5.0 * sc("A10"), "UNFORECAST", "S1",
         "Late notice of large redemption")

# S2: large sale fails to settle on value date, arrives 2 days late -> overdraft
SCEN["S2"] = dict(acct="A01", day=31, expect=["FORECAST_VARIANCE", "ACTUAL_OVERDRAFT"],
                  desc="Large sale proceeds fail to settle, arrive T+2 late")
fid = add_flow("A01", "TRADE_SELL", "Broker 07", 30, 31, 5.0 * sc("A01"), None, 0, "FAILED", "S2",
               "Counterparty fail on large sale")
add_flow("A01", "TRADE_SELL", "Broker 07", 32, 33, 5.0 * sc("A01"), 33, 5.0 * sc("A01"), "SETTLED", "S2",
         f"Rebooked fail of {fid}")

# S3: coupon paid net of 30% withholding tax (forecast was gross)
SCEN["S3"] = dict(acct="A07", day=27, expect=["FORECAST_VARIANCE"],
                  desc="Coupon received net of 30% withholding tax, forecast was gross")
add_flow("A07", "INCOME", "Paying agent", 17, 27, 3.0 * sc("A07"), 27, 2.1 * sc("A07"), "SETTLED", "S3",
         "Withholding tax deducted at source")

# S4: subscription booked twice in the forecast. The duplicate inflates the morning
#     projection, treasury sweeps out cash it does not have -> overdraft.
SCEN["S4"] = dict(acct="A12", day=40, expect=["DUPLICATE_FLOW", "FORECAST_VARIANCE", "ACTUAL_OVERDRAFT"],
                  desc="Subscription loaded twice from the TA file; duplicate caused an over-sweep")
dup_amt = 4.0 * sc("A12")
add_flow("A12", "SUBSCRIPTION", "Transfer Agent", 38, 40, dup_amt, 40, dup_amt, "SETTLED", "S4", "Large subscription")
add_flow("A12", "SUBSCRIPTION", "Transfer Agent", 38, 40, dup_amt, None, 0, "DUPLICATE", "S4",
         "Same deal loaded twice from TA file")

# S5: large subscription lands after the sweep cut-off -> idle cash overnight
SCEN["S5"] = dict(acct="A05", day=35, expect=["FORECAST_VARIANCE", "IDLE_CASH"],
                  desc="Large subscription received after the sweep cut-off, cash left idle")
add_flow("A05", "SUBSCRIPTION", "Transfer Agent", 35, 35, 0, 35, 7.0 * sc("A05"), "UNFORECAST", "S5",
         "Received after MMF sweep cut-off")

# S6: margin call from a clearing house, not forecast -> overdraft
SCEN["S6"] = dict(acct="A14", day=47, expect=["FORECAST_VARIANCE", "ACTUAL_OVERDRAFT"],
                  desc="Unforecast margin call from the clearing house")
add_flow("A14", "MISC", "Margin call - clearing house", 47, 47, 0, 47, -4.5 * sc("A14"), "UNFORECAST", "S6",
         "Variation margin call")

# S7: the control WORKING. Large redemption notified 5 days ahead: the ladder shows a
#     projected overdraft for 5 days, treasury funds it on the day, no actual overdraft.
SCEN["S7"] = dict(acct="A05", day=50, expect=[],
                  desc="Large redemption notified 5 days ahead; ladder warned, treasury funded it in time")
add_flow("A05", "REDEMPTION", "Transfer Agent", 45, 50, -6.0 * sc("A05"), 50, -6.0 * sc("A05"), "SETTLED", "S7",
         "Advance notice of large redemption")

# keep only flows that touch the window, then clip known dates to the calendar
FLOWS = [f for f in FLOWS if (0 <= f["fvd"] < N_ALL) or (f["avd"] is not None and 0 <= f["avd"] < N_ALL)]
for f in FLOWS:
    if f["avd"] is not None and f["avd"] >= N_ACTUAL:
        # beyond the last statement date: not settled yet from the desk's point of view
        f["avd"], f["aamt"] = None, 0
        if f["Status"] in ("SETTLED", "UNFORECAST"):
            f["Status"] = "PENDING"

# ---------------------------------------------------------------- engine
from collections import defaultdict

by_fvd = defaultdict(list)
by_avd = defaultdict(list)
for f in FLOWS:
    by_fvd[(f["AccountID"], f["fvd"])].append(f)
    if f["avd"] is not None:
        by_avd[(f["AccountID"], f["avd"])].append(f)


def forecast_on(aid, vd, asof):
    """Known-flow forecast for value date vd using only flows known by asof."""
    inflow = outflow = 0
    for f in by_fvd[(aid, vd)]:
        if f["known"] <= asof and f["FlowType"] != "SWEEP":
            if f["famt"] > 0:
                inflow += f["famt"]
            else:
                outflow += f["famt"]
    return inflow, outflow


def actual_on(aid, d):
    inflow = outflow = 0
    for f in by_avd[(aid, d)]:
        if f["FlowType"] != "SWEEP":
            if f["aamt"] > 0:
                inflow += f["aamt"]
            else:
                outflow += f["aamt"]
    return inflow, outflow


DAILY = []
opening_of = {}
action_of = {}
mmf = {aid: a["MMFOpening"] for aid, a in ACCT.items()}
closing_prev = {aid: a["TargetBalance"] for aid, a in ACCT.items()}

for d in range(N_ACTUAL):
    for aid, a in ACCT.items():
        opening = closing_prev[aid]
        fin, fout = forecast_on(aid, d, d)
        fnet = fin + fout
        pre = opening + fnet
        if pre < a["MinBalance"]:
            action, atype = round_away(a["TargetBalance"] - pre), "FUND FROM MMF"
        elif pre > a["IdleThreshold"]:
            action, atype = round_away(a["TargetBalance"] - pre), "SWEEP TO MMF"
        else:
            action, atype = 0, "NONE"
        if action != 0:
            add_flow(aid, "SWEEP", "Liquidity fund (MMF)", d, d, action, d, action, "SETTLED", "Treasury",
                     atype.title())
        ain, aout = actual_on(aid, d)
        anet = ain + aout
        closing = opening + action + anet
        mmf[aid] -= action
        opening_of[(aid, d)] = opening
        action_of[(aid, d)] = action
        closing_prev[aid] = closing

        gross = ain - aout
        variance = anet - fnet
        tol = max(a["VarianceFloor"], round_away(0.05 * gross, 0))
        r = fx(a["Currency"], d)
        basis = a["DayBasis"]
        DAILY.append(dict(
            Date=date_of(d), d=d, AccountID=aid, Currency=a["Currency"],
            Opening=opening, ForecastInflows=fin, ForecastOutflows=fout, ForecastNetFlow=fnet,
            PreActionProjected=pre, TreasuryAction=action, ActionType=atype,
            ActualInflows=ain, ActualOutflows=aout, ActualNetFlow=anet, GrossFlows=gross,
            Closing=closing, Variance=variance, Tolerance=tol,
            IsOverdraft=int(closing < 0), IsBelowBuffer=int(0 <= closing < a["MinBalance"]),
            IsIdle=int(closing > a["IdleThreshold"]), IsVarianceBreach=int(abs(variance) > tol),
            MMFBalance=mmf[aid], FXRate=r,
            OpeningUSD=round(opening * r, 2), ClosingUSD=round(closing * r, 2),
            ForecastNetFlowUSD=round(fnet * r, 2), ActualNetFlowUSD=round(anet * r, 2),
            VarianceUSD=round(variance * r, 2), GrossFlowsUSD=round(gross * r, 2),
            MMFBalanceUSD=round(mmf[aid] * r, 2),
            OverdraftCostUSD=round(max(0, -closing) * a["OverdraftRatePct"] / 100 / basis * r, 2),
            IdleDragUSD=round(max(0, closing - a["TargetBalance"]) * a["MMFYieldPct"] / 100 / basis * r, 2),
        ))

# rebuild indices now that sweeps exist (sweeps are excluded from forecast/actual sums anyway)
FLOW_BY_ID = {f["FlowID"]: f for f in FLOWS}

# ---------------------------------------------------------------- root cause per account-day


def root_cause(aid, d):
    """Largest single-flow contribution to (actual - forecast) on day d."""
    contrib = {}
    for f in by_fvd[(aid, d)]:
        if f["known"] <= d and f["FlowType"] != "SWEEP":
            contrib[f["FlowID"]] = contrib.get(f["FlowID"], 0) - f["famt"]
    for f in by_avd[(aid, d)]:
        if f["FlowType"] != "SWEEP":
            contrib[f["FlowID"]] = contrib.get(f["FlowID"], 0) + f["aamt"]
    if not contrib:
        return "", ""
    fid, val = max(contrib.items(), key=lambda kv: abs(kv[1]))
    f = FLOW_BY_ID[fid]
    if f["Status"] == "DUPLICATE":
        cause = "Duplicate forecast entry"
    elif f["Status"] == "FAILED":
        cause = "Settlement fail"
    elif f["famt"] == 0:
        cause = "Unforecast flow"
    elif f["known"] > d:
        cause = "Late notice"
    else:
        cause = "Amount variance"
    return cause, fid


for row in DAILY:
    if row["IsVarianceBreach"] or row["IsOverdraft"] or row["IsIdle"]:
        row["RootCause"], row["RootFlowID"] = root_cause(row["AccountID"], row["d"])
    else:
        row["RootCause"], row["RootFlowID"] = "", ""
    row["Status"] = "EXCEPTION" if (row["IsVarianceBreach"] or row["IsOverdraft"] or row["IsIdle"]) else "OK"

# ---------------------------------------------------------------- ladder
LADDER = []
for a_i in range(N_ACTUAL):
    for aid, a in ACCT.items():
        r = fx(a["Currency"], a_i)
        proj = opening_of[(aid, a_i)]
        for h in range(MAX_H + 1):
            vd = a_i + h
            popen = proj
            fin, fout = forecast_on(aid, vd, a_i)
            act = action_of[(aid, a_i)] if h == 0 else 0
            proj = popen + fin + fout + act
            if h == 0:
                pre = popen + fin + fout
                flag = ("ACTION: FUND FROM MMF" if act > 0 else
                        "ACTION: SWEEP TO MMF" if act < 0 else "OK")
            else:
                # future days are a funding PLAN, not exceptions: treasury acts on them when they reach T+0
                flag = ("FUNDING NEED" if proj < a["MinBalance"] else
                        "SURPLUS TO SWEEP" if proj > a["IdleThreshold"] else "OK")
            has_actual = int(vd < N_ACTUAL)
            if has_actual:
                ain, aout = actual_on(aid, vd)
                anet, agross = ain + aout, ain - aout
            else:
                anet = agross = 0
            LADDER.append(dict(
                AsOfDate=date_of(a_i), AccountID=aid, Currency=a["Currency"], Horizon=h, HorizonLabel=f"T+{h}",
                ValueDate=date_of(vd), ProjOpening=popen, ProjInflows=fin, ProjOutflows=fout,
                ProjNetFlow=fin + fout, TreasuryAction=act, ProjClosing=proj, LadderFlag=flag,
                FXRate=r, ProjClosingUSD=round(proj * r, 2), ProjNetFlowUSD=round((fin + fout) * r, 2),
                FundingNeedUSD=round(max(0, a["TargetBalance"] - proj) * r, 2) if flag == "FUNDING NEED" else 0,
                SurplusUSD=round(max(0, proj - a["TargetBalance"]) * r, 2) if flag == "SURPLUS TO SWEEP" else 0,
                HasActual=has_actual,
                ActualNetFlow=anet if has_actual else "",
                ActualGross=agross if has_actual else "",
                ActualNetFlowUSD=round(anet * r, 2) if has_actual else "",
                AbsErrorUSD=round(abs(anet - (fin + fout)) * r, 2) if has_actual else "",
                ActualGrossUSD=round(agross * r, 2) if has_actual else "",
            ))

# ---------------------------------------------------------------- exceptions register
EXC = []
scen_of_flow = {f["FlowID"]: f["Scenario"] for f in FLOWS}


def suggest(etype, cause):
    return {
        "ACTUAL_OVERDRAFT": "Fund account from MMF / credit line today; chase root cause",
        "IDLE_CASH": "Sweep excess to MMF at next cut-off",
        "DUPLICATE_FLOW": "Remove duplicate from forecast before next ladder run",
    }.get(etype, {
        "Settlement fail": "Chase counterparty, rebook expected value date",
        "Unforecast flow": "Obtain notice earlier; review dealing / margin feeds",
        "Amount variance": "Reconcile amount with paying agent / tax status",
        "Duplicate forecast entry": "Remove duplicate from forecast",
        "Late notice": "Escalate late notice with transfer agent",
    }.get(cause, "Investigate"))


for row in DAILY:
    a = ACCT[row["AccountID"]]
    src = scen_of_flow.get(row["RootFlowID"], "")
    for etype, cond, amt, sev in [
        ("ACTUAL_OVERDRAFT", row["IsOverdraft"], row["Closing"], "High"),
        ("IDLE_CASH", row["IsIdle"], row["Closing"] - a["TargetBalance"], "Medium"),
        ("FORECAST_VARIANCE", row["IsVarianceBreach"], row["Variance"],
         "High" if abs(row["Variance"]) > 3 * row["Tolerance"] else "Medium"),
    ]:
        if cond:
            EXC.append(dict(Date=row["Date"], AccountID=row["AccountID"], ExceptionType=etype, Severity=sev,
                            AmountLocal=amt, AmountUSD=round(amt * row["FXRate"], 2),
                            RootCause=row["RootCause"], RootFlowID=row["RootFlowID"],
                            Source=src if src.startswith("S") else "Natural",
                            SuggestedAction=suggest(etype, row["RootCause"])))

# duplicate detection on the forecast book: same account, type, counterparty, value date, amount
seen = defaultdict(list)
for f in FLOWS:
    if f["FlowType"] != "SWEEP" and f["famt"] != 0:
        seen[(f["AccountID"], f["FlowType"], f["Counterparty"], f["fvd"], f["famt"])].append(f)
for key, grp in seen.items():
    if len(grp) > 1:
        g = sorted(grp, key=lambda x: x["FlowID"])
        for extra in g[1:]:
            d = max(extra["known"], 0)
            if d >= N_ACTUAL:
                continue
            r = fx(ACCT[extra["AccountID"]]["Currency"], d)
            EXC.append(dict(Date=date_of(d), AccountID=extra["AccountID"], ExceptionType="DUPLICATE_FLOW",
                            Severity="High", AmountLocal=extra["famt"], AmountUSD=round(extra["famt"] * r, 2),
                            RootCause="Duplicate forecast entry", RootFlowID=extra["FlowID"],
                            Source=extra["Scenario"] if extra["Scenario"].startswith("S") else "Natural",
                            SuggestedAction=suggest("DUPLICATE_FLOW", "")))

EXC.sort(key=lambda e: (e["Date"], e["AccountID"], e["ExceptionType"]))
for i, e in enumerate(EXC, 1):
    e["ExceptionID"] = f"EX{i:03d}"
    e["FundName"] = ACCT[e["AccountID"]]["FundName"]
    e["Currency"] = ACCT[e["AccountID"]]["Currency"]
    e["Bank"] = ACCT[e["AccountID"]]["Bank"]

# ---------------------------------------------------------------- write CSVs


def write(name, rows, cols):
    with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


acct_cols = ["AccountID", "FundID", "FundName", "FundBaseCcy", "Currency", "Bank", "AccountType",
             "MinBalance", "TargetBalance", "IdleThreshold", "VarianceFloor", "OverdraftRatePct",
             "MMFYieldPct", "DayBasis", "MMFOpening"]
write("dim_account.csv", ACCT.values(), acct_cols)

flow_rows = []
for f in FLOWS:
    a = ACCT[f["AccountID"]]
    rk = fx(a["Currency"], max(f["known"], -20))
    flow_rows.append(dict(
        FlowID=f["FlowID"], AccountID=f["AccountID"], FlowType=f["FlowType"], Counterparty=f["Counterparty"],
        KnownDate=date_of(f["known"]), ForecastValueDate=date_of(f["fvd"]), ForecastAmount=f["famt"],
        ActualValueDate=date_of(f["avd"]) if f["avd"] is not None else "",
        ActualAmount=f["aamt"], Status=f["Status"], Scenario=f["Scenario"], Note=f["Note"],
        ForecastAmountUSD=round(f["famt"] * fx(a["Currency"], f["fvd"]), 2),
        ActualAmountUSD=round(f["aamt"] * fx(a["Currency"], f["avd"]), 2) if f["avd"] is not None else 0,
    ))
flow_rows.sort(key=lambda r: r["FlowID"])
write("cash_flows.csv", flow_rows, list(flow_rows[0].keys()))

daily_cols = ["Date", "AccountID", "Currency", "Opening", "ForecastInflows", "ForecastOutflows", "ForecastNetFlow",
              "PreActionProjected", "TreasuryAction", "ActionType", "ActualInflows", "ActualOutflows",
              "ActualNetFlow", "GrossFlows", "Closing", "Variance", "Tolerance", "IsOverdraft", "IsBelowBuffer",
              "IsIdle", "IsVarianceBreach", "Status", "RootCause", "RootFlowID", "MMFBalance", "FXRate",
              "OpeningUSD", "ClosingUSD", "ForecastNetFlowUSD", "ActualNetFlowUSD", "VarianceUSD", "GrossFlowsUSD",
              "MMFBalanceUSD", "OverdraftCostUSD", "IdleDragUSD"]
write("daily_position.csv", DAILY, daily_cols)
write("cash_ladder.csv", LADDER, list(LADDER[0].keys()))
exc_cols = ["ExceptionID", "Date", "AccountID", "FundName", "Currency", "Bank", "ExceptionType", "Severity",
            "AmountLocal", "AmountUSD", "RootCause", "RootFlowID", "Source", "SuggestedAction"]
write("exceptions.csv", EXC, exc_cols)

fx_rows = [dict(Date=date_of(i), Currency=c, USDPerUnit=fx(c, i)) for i in range(N_ALL) for c in CCY]
write("fx_rates.csv", fx_rows, ["Date", "Currency", "USDPerUnit"])

with open(os.path.join(OUT, "_scenarios.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["Scenario", "AccountID", "Date", "ExpectedExceptions", "Description"])
    for k, s in SCEN.items():
        w.writerow([k, s["acct"], date_of(s["day"]), "|".join(s["expect"]), s["desc"]])

with open(os.path.join(HERE, "run_summary.txt"), "w", encoding="utf-8") as fh:
    fh.write(f"flows={len(FLOWS)} daily={len(DAILY)} ladder={len(LADDER)} exceptions={len(EXC)}\n")
    fh.write(f"min MMF balance (local, by acct): " +
             ", ".join(f"{k}:{min(r['MMFBalance'] for r in DAILY if r['AccountID']==k)/ACCT[k]['scale']:.1f}x"
                       for k in ACCT) + "\n")
    from collections import Counter
    fh.write("exceptions by type/source: " + str(Counter((e["ExceptionType"], e["Source"]) for e in EXC)) + "\n")
    fh.write("sweep actions: " + str(Counter(r["ActionType"] for r in DAILY)) + "\n")
    fh.write("ladder flags: " + str(Counter(r["LadderFlag"] for r in LADDER)) + "\n")
print("done")
