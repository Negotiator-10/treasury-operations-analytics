"""
Engine 3: FX Share-Class Hedging and Bank Counterparty Exposure
Treasury Operations Analytics (synthetic data)

Part A, share-class hedging. A currency-hedged share class (for example the EUR
class of a USD fund) sells the fund's base currency forward so investors do not
carry the base/class currency risk. The hedge notional is fixed between trades
while the class NAV moves every day, so the hedge ratio drifts:
    hedge ratio = outstanding forward notional / class NAV   (both in base ccy)
Policy used here:
    target 100%; soft band 97% to 103% -> rebalance next business day;
    hard limit 95% to 105% (the ESMA share-class guideline) -> breach;
    1-month forwards rolled on the last business day of each month.

Part B, counterparty exposure. Every bank we face is a credit risk:
    custody banks: cash deposits (from Engine 1 closing balances)
    FX banks: positive net mark-to-market of our forwards with them
Exposure is compared with a rating-based limit (warning at 90%), and the share
of forward notional with any one FX bank must stay at or below 50%.

Scenarios H1 to H4 are injected so the checks can prove each control fires.
Same 60 business days and FX rates as Engine 1.
"""
import csv
import datetime as dt
import os
from collections import defaultdict, Counter
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data")
E1 = os.path.join(HERE, "..", "..", "01_Cash_Position_Forecast", "data")
rng = np.random.default_rng(20260929)

START = np.datetime64("2026-06-01")
N = 60


def bd(i):
    return dt.date.fromisoformat(str(np.busday_offset(START, int(i), roll="forward")))


def load(p):
    with open(p, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


ACC = {a["AccountID"]: a for a in load(os.path.join(E1, "dim_account.csv"))}
FXR = {(r["Date"], r["Currency"]): float(r["USDPerUnit"]) for r in load(os.path.join(E1, "fx_rates.csv"))}
E1_DAILY = load(os.path.join(E1, "daily_position.csv"))
BASE_ACCT = {a["FundID"]: aid for aid, a in ACC.items() if a["Currency"] == a["FundBaseCcy"]}
FUND_NAME = {a["FundID"]: a["FundName"] for a in ACC.values()}
FUND_BASE = {a["FundID"]: a["FundBaseCcy"] for a in ACC.values()}
RATE = {"USD": 3.75, "EUR": 2.00, "GBP": 3.75, "JPY": 0.50, "INR": 5.50}   # same policy rates as Engine 1

SOFT_LO, SOFT_HI, HARD_LO, HARD_HI = 0.97, 1.03, 0.95, 1.05
CONC_LIMIT, WARN = 0.50, 0.90


def usd(ccy, d):
    return FXR[(bd(d).isoformat(), ccy)]


def cross(base, cls, d):
    """units of class currency per 1 unit of base currency"""
    return usd(base, d) / usd(cls, d)


def fwd_rate(base, cls, d, mat):
    tau = (bd(mat) - bd(d)).days if isinstance(mat, int) else (mat - bd(d)).days
    return cross(base, cls, d) * (1 + RATE[cls] / 100 * tau / 360) / (1 + RATE[base] / 100 * tau / 360)


# ---------------------------------------------------------------- reference data
CLASSES = [
    # ClassID, Fund, class ccy, primary FX bank, initial class NAV in USD
    ("C01", "F01", "EUR", "FX Bank 1", 400e6),
    ("C02", "F01", "GBP", "FX Bank 2", 150e6),
    ("C03", "F01", "JPY", "FX Bank 3", 100e6),
    ("C04", "F02", "EUR", "FX Bank 1", 250e6),
    ("C05", "F02", "GBP", "FX Bank 2", 120e6),
    ("C06", "F03", "USD", "FX Bank 3", 300e6),
    ("C07", "F03", "GBP", "FX Bank 1", 80e6),
    ("C08", "F04", "USD", "FX Bank 2", 200e6),
    ("C09", "F04", "EUR", "FX Bank 3", 90e6),
    ("C10", "F05", "USD", "FX Bank 3", 180e6),
    ("C11", "F05", "EUR", "FX Bank 2", 70e6),
]
CLS = {c[0]: dict(ClassID=c[0], FundID=c[1], FundName=FUND_NAME[c[1]], BaseCcy=FUND_BASE[c[1]], ClassCcy=c[2],
                  ClassName=f"{FUND_NAME[c[1]]} {c[2]} Hedged", PrimaryCounterparty=c[3], InitialNAVUSD=c[4])
       for c in CLASSES}
FUND_VOL = {"F01": 0.010, "F02": 0.003, "F03": 0.003, "F04": 0.009, "F05": 0.011}

CPTY = [
    # name, type, rating, limit USD (illustrative, sized to typical balances / notional)
    ("Bank A", "Custody bank", "AA-", 70e6), ("Bank B", "Custody bank", "A+", 35e6),
    ("Bank C", "Custody bank", "A", 45e6), ("Bank D", "Custody bank", "A+", 35e6),
    ("Bank E", "Custody bank", "A", 25e6), ("Bank F", "Custody bank", "BBB+", 20e6),
    ("FX Bank 1", "FX counterparty", "AA-", 40e6), ("FX Bank 2", "FX counterparty", "A+", 30e6),
    ("FX Bank 3", "FX counterparty", "A", 25e6),
]
LIMIT = {c[0]: c[3] for c in CPTY}
FX_BANKS = [c[0] for c in CPTY if c[1] == "FX counterparty"]

month_end = [i for i in range(N + 10) if bd(i + 1).month != bd(i).month]      # last business day of month
ROLL_DAYS = [d for d in month_end if d < N]                                     # 2026-06-30, 2026-07-31
MAT_AFTER = {0: month_end[0]}
for k, d in enumerate(month_end[:-1]):
    MAT_AFTER[d] = month_end[k + 1]

# ---------------------------------------------------------------- scenarios
SCEN = {
    "H1": dict(entity="C01", day=16, expect="HEDGE_RATIO_BREACH",
               desc="12% of the EUR hedged class redeemed in one day: hedge suddenly over 105%"),
    "H2": dict(entity="C08", day=35, expect="MISSED_REBALANCE",
               desc="Rebalance trades not done for 3 days (desk short-staffed); ratio drifts below 95%"),
    "H3": dict(entity="Bank C", day=35, expect="COUNTERPARTY_LIMIT_BREACH",
               desc="From Engine 1 S5: late USD 42m subscription left idle at Bank C over its limit"),
    "H4": dict(entity="FX Bank 2", day=ROLL_DAYS[1], expect="CONCENTRATION_BREACH",
               desc="July roll moved FX Bank 1 classes to FX Bank 2 for a better quote: over 50% of notional"),
}
FLOW_OVERRIDE = {("C01", 16): -0.12, ("C08", 34): 0.05, ("C08", 36): 0.04}
SKIP_REBAL = {("C08", 35), ("C08", 36), ("C08", 37)}

# ---------------------------------------------------------------- simulation
CONTRACTS = []
_k = [0]


def trade(cid, d, notional, ctype, cpty, mat):
    c = CLS[cid]
    _k[0] += 1
    CONTRACTS.append(dict(ContractID=f"FW{_k[0]:04d}", ClassID=cid, FundID=c["FundID"], Counterparty=cpty,
                          TradeDate=bd(d), MaturityDate=bd(mat), mat=mat, trade_day=d,
                          SellCcy=c["BaseCcy"], BuyCcy=c["ClassCcy"], NotionalBase=round(notional, 2),
                          ForwardRate=round(fwd_rate(c["BaseCcy"], c["ClassCcy"], d, mat), 8), Type=ctype,
                          SettledDate=None, SettlementBase=None))


fund_ret = {f: rng.normal(0.0003, v, N) for f, v in FUND_VOL.items()}
nav = {cid: c["InitialNAVUSD"] / usd(c["BaseCcy"], 0) for cid, c in CLS.items()}
cur_mat = MAT_AFTER[0]
cpty_of = {cid: c["PrimaryCounterparty"] for cid, c in CLS.items()}
for cid in CLS:
    trade(cid, 0, nav[cid], "Initial", cpty_of[cid], cur_mat)

HEDGE, SETTLE = [], []
prev_ratio = {cid: 1.0 for cid in CLS}
for d in range(N):
    # 1) maturities settle (cash P&L in base currency) and the book rolls
    if d in ROLL_DAYS:
        for k in CONTRACTS:
            if k["mat"] == d and k["SettledDate"] is None:
                c = CLS[k["ClassID"]]
                pnl = k["NotionalBase"] * (k["ForwardRate"] / cross(c["BaseCcy"], c["ClassCcy"], d) - 1)
                k["SettledDate"], k["SettlementBase"] = bd(d), round(pnl, 2)
        cur_mat = MAT_AFTER[d]
        if d == SCEN["H4"]["day"]:
            for cid in CLS:
                if cpty_of[cid] == "FX Bank 1":
                    cpty_of[cid] = "FX Bank 2"
        for cid in CLS:
            trade(cid, d, nav[cid], "Roll", cpty_of[cid], cur_mat)       # reset to 100% of last known NAV
    # 2) rebalance on yesterday's ratio if outside the soft band (not on roll days, already reset)
    rebal = {}
    if d > 0 and d not in ROLL_DAYS:
        for cid in CLS:
            if not (SOFT_LO <= prev_ratio[cid] <= SOFT_HI) and (cid, d) not in SKIP_REBAL:
                held = sum(k["NotionalBase"] for k in CONTRACTS if k["ClassID"] == cid and k["SettledDate"] is None)
                adj = nav[cid] - held
                trade(cid, d, adj, "Rebalance", cpty_of[cid], cur_mat)
                rebal[cid] = adj
    # 3) today's NAV: market return + class flows
    for cid, c in CLS.items():
        flow = FLOW_OVERRIDE.get((cid, d))
        if flow is None:
            flow = rng.normal(0, 0.002) + (rng.choice([-1, 1]) * rng.uniform(0.01, 0.03) if rng.random() < 0.04 else 0)
        mret = float(fund_ret[c["FundID"]][d])
        nav_prev = nav[cid]
        nav[cid] = nav[cid] * (1 + mret) + nav_prev * flow
        live = [k for k in CONTRACTS if k["ClassID"] == cid and k["SettledDate"] is None and k["trade_day"] <= d]
        held = sum(k["NotionalBase"] for k in live)
        ratio = held / nav[cid]
        mtm_base = sum(k["NotionalBase"] * (k["ForwardRate"] / fwd_rate(c["BaseCcy"], c["ClassCcy"], d, k["mat"]) - 1)
                       for k in live)
        missed = int(d > 0 and d not in ROLL_DAYS and not (SOFT_LO <= prev_ratio[cid] <= SOFT_HI) and cid not in rebal)
        status = ("HARD BREACH OVER" if ratio > HARD_HI else "HARD BREACH UNDER" if ratio < HARD_LO else
                  "REBALANCE DUE" if not (SOFT_LO <= ratio <= SOFT_HI) else "OK")
        r = usd(c["BaseCcy"], d)
        HEDGE.append(dict(Date=bd(d), d=d, ClassID=cid, FundID=c["FundID"], BaseCcy=c["BaseCcy"], ClassCcy=c["ClassCcy"],
                          MarketReturn=round(mret, 6), ClassFlowPct=round(flow, 6),
                          ClassNAVBase=round(nav[cid], 2), HedgeNotionalBase=round(held, 2),
                          HedgeRatio=round(ratio, 6), BandStatus=status,
                          RebalanceNotionalBase=round(rebal.get(cid, 0), 2), IsRebalanceDay=int(cid in rebal),
                          IsRollDay=int(d in ROLL_DAYS), IsSoftTrigger=int(not (SOFT_LO <= ratio <= SOFT_HI)),
                          IsHardBreach=int(ratio > HARD_HI or ratio < HARD_LO), IsMissedRebalance=missed,
                          FXRateUSD=r, ClassNAVUSD=round(nav[cid] * r, 2), HedgeNotionalUSD=round(held * r, 2),
                          MTMBase=round(mtm_base, 2), MTMUSD=round(mtm_base * r, 2),
                          Counterparty=cpty_of[cid]))
        prev_ratio[cid] = ratio

# roll settlements vs the fund's cash buffer (Engine 1 base-account target balance)
for d in ROLL_DAYS:
    per_fund = defaultdict(float)
    for k in CONTRACTS:
        if k["SettledDate"] == bd(d):
            per_fund[k["FundID"]] += k["SettlementBase"]
    for f, amt in sorted(per_fund.items()):
        a = ACC[BASE_ACCT[f]]
        tgt = float(a["TargetBalance"])
        SETTLE.append(dict(Date=bd(d), FundID=f, FundName=FUND_NAME[f], BaseCcy=FUND_BASE[f], BaseAccount=BASE_ACCT[f],
                           SettlementBase=round(amt, 2), SettlementUSD=round(amt * usd(FUND_BASE[f], d), 2),
                           AccountTargetBase=tgt, ExceedsCashBuffer=int(amt < 0 and -amt > tgt)))

# ---------------------------------------------------------------- counterparty exposure
bank_of = {aid: a["Bank"] for aid, a in ACC.items()}
cash = defaultdict(float)
for r in E1_DAILY:
    cash[(r["Date"], bank_of[r["AccountID"]])] += max(0.0, float(r["ClosingUSD"]))
mtm = defaultdict(float)
notional = defaultdict(float)
for d in range(N):
    for k in CONTRACTS:
        if k["trade_day"] <= d and (k["SettledDate"] is None or k["mat"] > d):
            c = CLS[k["ClassID"]]
            m = k["NotionalBase"] * (k["ForwardRate"] / fwd_rate(c["BaseCcy"], c["ClassCcy"], d, k["mat"]) - 1)
            mtm[(d, k["Counterparty"])] += m * usd(c["BaseCcy"], d)
            notional[(d, k["Counterparty"])] += k["NotionalBase"] * usd(c["BaseCcy"], d)

CPD = []
for d in range(N):
    tot_notional = sum(notional[(d, b)] for b in FX_BANKS)
    for name, ctype, rating, lim in CPTY:
        c_usd = cash[(bd(d).isoformat(), name)] if ctype == "Custody bank" else 0.0
        m_usd = mtm[(d, name)]
        expo = c_usd + max(0.0, m_usd)
        util = expo / lim
        share = notional[(d, name)] / tot_notional if ctype == "FX counterparty" else None
        CPD.append(dict(Date=bd(d), d=d, Counterparty=name, CounterpartyType=ctype, Rating=rating, LimitUSD=lim,
                        CashUSD=round(c_usd, 2), NetMTMUSD=round(m_usd, 2), ExposureUSD=round(expo, 2),
                        Utilization=round(util, 6), ForwardNotionalUSD=round(notional[(d, name)], 2),
                        NotionalShare=round(share, 6) if share is not None else "",
                        LimitStatus="BREACH" if util > 1 else "WARNING" if util > WARN else "OK",
                        IsLimitBreach=int(util > 1), IsLimitWarning=int(WARN < util <= 1),
                        IsConcentrationBreach=int(share is not None and share > CONC_LIMIT)))

# ---------------------------------------------------------------- exceptions register (episodes)
EXC = []


def episodes(rows, key, flag):
    """group consecutive business days with flag=1 per entity into one episode"""
    by = defaultdict(list)
    for r in rows:
        if r[flag]:
            by[r[key]].append(r)
    out = []
    for ent, rs in by.items():
        rs.sort(key=lambda r: r["d"])
        cur = [rs[0]]
        for r in rs[1:]:
            if r["d"] == cur[-1]["d"] + 1:
                cur.append(r)
            else:
                out.append((ent, cur))
                cur = [r]
        out.append((ent, cur))
    return out


def source(etype, ent, start_d):
    for k, s in SCEN.items():
        if s["expect"] == etype and s["entity"] == ent and abs(s["day"] - start_d) <= 2:
            return k
    if etype == "HEDGE_RATIO_BREACH" and ent == "C08" and 34 <= start_d <= 38:
        return "H2"
    return "Natural"


def add(etype, ent, rs, sev, peak, detail, action):
    EXC.append(dict(ExceptionType=etype, Entity=ent, StartDate=rs[0]["Date"], EndDate=rs[-1]["Date"], Days=len(rs),
                    Severity=sev, Peak=peak, Detail=detail, Source=source(etype, ent, rs[0]["d"]),
                    Status="Open" if rs[-1]["d"] == N - 1 else "Closed", SuggestedAction=action))


for ent, rs in episodes(HEDGE, "ClassID", "IsHardBreach"):
    pk = max(rs, key=lambda r: abs(r["HedgeRatio"] - 1))
    driver = ("class flow" if abs(pk["ClassFlowPct"]) > abs(pk["MarketReturn"]) else "market move")
    add("HEDGE_RATIO_BREACH", ent, rs, "High", f"{pk['HedgeRatio']:.1%}",
        f"Hedge ratio {pk['HedgeRatio']:.1%} outside 95% to 105%; biggest driver on the day: {driver}",
        "Rebalance today; review why the soft trigger did not prevent it")
for ent, rs in episodes(HEDGE, "ClassID", "IsMissedRebalance"):
    add("MISSED_REBALANCE", ent, rs, "High", f"{min(r['HedgeRatio'] for r in rs):.1%}",
        "Ratio outside 97% to 103% at prior close but no rebalance trade booked",
        "Book the rebalance; add a back-up owner for the hedge desk")
for ent, rs in episodes(CPD, "Counterparty", "IsLimitBreach"):
    pk = max(rs, key=lambda r: r["Utilization"])
    add("COUNTERPARTY_LIMIT_BREACH", ent, rs, "High", f"{pk['Utilization']:.0%}",
        f"Exposure USD {pk['ExposureUSD']/1e6:,.1f}m vs limit USD {pk['LimitUSD']/1e6:,.0f}m",
        "Move cash to another bank or MMF / novate or reduce forwards")
for ent, rs in episodes(CPD, "Counterparty", "IsLimitWarning"):
    pk = max(rs, key=lambda r: r["Utilization"])
    add("COUNTERPARTY_LIMIT_WARNING", ent, rs, "Medium", f"{pk['Utilization']:.0%}",
        f"Exposure USD {pk['ExposureUSD']/1e6:,.1f}m, above 90% of USD {pk['LimitUSD']/1e6:,.0f}m limit",
        "Plan to move cash before the next large inflow")
for ent, rs in episodes(CPD, "Counterparty", "IsConcentrationBreach"):
    pk = max(rs, key=lambda r: r["NotionalShare"])
    add("CONCENTRATION_BREACH", ent, rs, "High", f"{pk['NotionalShare']:.0%}",
        f"{pk['NotionalShare']:.0%} of all forward notional with one bank (limit 50%)",
        "Split next roll across FX banks")
for s in SETTLE:
    if s["ExceedsCashBuffer"]:
        rs = [dict(Date=s["Date"], d=int(np.busday_count(START, np.datetime64(s["Date"]))))]
        add("ROLL_CASH_CALL", s["FundID"], rs, "Medium", f"USD {-s['SettlementUSD']/1e6:,.1f}m",
            f"Roll settlement outflow {s['BaseCcy']} {-s['SettlementBase']:,.0f} exceeds base account target "
            f"{s['BaseCcy']} {s['AccountTargetBase']:,.0f}", "Pre-fund in the Engine 1 cash ladder two days ahead")

EXC.sort(key=lambda e: (e["StartDate"], e["ExceptionType"], e["Entity"]))
for i, e in enumerate(EXC, 1):
    e["ExceptionID"] = f"HX{i:03d}"

# ---------------------------------------------------------------- write


def fmt(v):
    return v.isoformat() if isinstance(v, dt.date) else ("" if v is None else v)


def write(name, rows, cols):
    with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([fmt(r.get(c)) for c in cols])


write("dim_share_class.csv", CLS.values(), ["ClassID", "ClassName", "FundID", "FundName", "BaseCcy", "ClassCcy",
                                            "PrimaryCounterparty", "InitialNAVUSD"])
write("dim_counterparty.csv", [dict(Counterparty=c[0], CounterpartyType=c[1], Rating=c[2], LimitUSD=c[3]) for c in CPTY],
      ["Counterparty", "CounterpartyType", "Rating", "LimitUSD"])
write("fx_forwards.csv", CONTRACTS, ["ContractID", "ClassID", "FundID", "Counterparty", "Type", "TradeDate", "MaturityDate",
                                     "SellCcy", "BuyCcy", "NotionalBase", "ForwardRate", "SettledDate", "SettlementBase"])
write("hedge_daily.csv", HEDGE, ["Date", "ClassID", "FundID", "BaseCcy", "ClassCcy", "MarketReturn", "ClassFlowPct",
                                 "ClassNAVBase", "HedgeNotionalBase", "HedgeRatio", "BandStatus", "RebalanceNotionalBase",
                                 "IsRebalanceDay", "IsRollDay", "IsSoftTrigger", "IsHardBreach", "IsMissedRebalance",
                                 "FXRateUSD", "ClassNAVUSD", "HedgeNotionalUSD", "MTMBase", "MTMUSD", "Counterparty"])
write("counterparty_daily.csv", CPD, ["Date", "Counterparty", "CounterpartyType", "Rating", "LimitUSD", "CashUSD",
                                      "NetMTMUSD", "ExposureUSD", "Utilization", "ForwardNotionalUSD", "NotionalShare",
                                      "LimitStatus", "IsLimitBreach", "IsLimitWarning", "IsConcentrationBreach"])
write("roll_settlements.csv", SETTLE, ["Date", "FundID", "FundName", "BaseCcy", "BaseAccount", "SettlementBase",
                                       "SettlementUSD", "AccountTargetBase", "ExceedsCashBuffer"])
write("exceptions.csv", EXC, ["ExceptionID", "ExceptionType", "Entity", "StartDate", "EndDate", "Days", "Severity", "Peak",
                              "Detail", "Source", "Status", "SuggestedAction"])
with open(os.path.join(OUT, "_scenarios.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["Scenario", "Entity", "Date", "ExpectedException", "Description"])
    for k, s in SCEN.items():
        w.writerow([k, s["entity"], bd(s["day"]).isoformat(), s["expect"], s["desc"]])

with open(os.path.join(HERE, "run_summary.txt"), "w", encoding="utf-8") as fh:
    fh.write(f"contracts={len(CONTRACTS)} hedge rows={len(HEDGE)} cpty rows={len(CPD)} exceptions={len(EXC)}\n")
    fh.write("contract types: " + str(Counter(k["Type"] for k in CONTRACTS)) + "\n")
    fh.write("band status: " + str(Counter(h["BandStatus"] for h in HEDGE)) + "\n")
    fh.write("exceptions: " + str(Counter((e["ExceptionType"], e["Source"]) for e in EXC)) + "\n")
    for e in EXC:
        fh.write(f"  {e['ExceptionID']} {e['ExceptionType']} {e['Entity']} {e['StartDate']}..{e['EndDate']} "
                 f"{e['Days']}d peak {e['Peak']} [{e['Source']}]\n")
    fh.write("roll settlements: " + "; ".join(f"{s['Date']} {s['FundID']} USD {s['SettlementUSD']/1e6:,.2f}m"
                                              for s in SETTLE) + "\n")
    for b in FX_BANKS:
        fh.write(f"{b}: max util {max(r['Utilization'] for r in CPD if r['Counterparty']==b):.0%}, "
                 f"share range {min(r['NotionalShare'] for r in CPD if r['Counterparty']==b):.0%} to "
                 f"{max(r['NotionalShare'] for r in CPD if r['Counterparty']==b):.0%}\n")
print("done")
