"""
Independent checks for Engine 3. Reads ONLY the output CSVs plus Engine 1's
files, and rebuilds hedge notionals, ratios, forward prices, mark-to-market,
roll settlements and bank exposure without importing the engine.
Writes verify_report.txt; exits non-zero if any gate fails.
"""
import csv
import datetime as dt
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
E1 = os.path.join(HERE, "..", "..", "01_Cash_Position_Forecast", "data")


def load(p):
    with open(p, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


cls = {c["ClassID"]: c for c in load(os.path.join(DATA, "dim_share_class.csv"))}
cpty = {c["Counterparty"]: c for c in load(os.path.join(DATA, "dim_counterparty.csv"))}
fw = load(os.path.join(DATA, "fx_forwards.csv"))
hd = load(os.path.join(DATA, "hedge_daily.csv"))
cp = load(os.path.join(DATA, "counterparty_daily.csv"))
rs = load(os.path.join(DATA, "roll_settlements.csv"))
exc = load(os.path.join(DATA, "exceptions.csv"))
scen = load(os.path.join(DATA, "_scenarios.csv"))
acc = {a["AccountID"]: a for a in load(os.path.join(E1, "dim_account.csv"))}
e1 = load(os.path.join(E1, "daily_position.csv"))
fx = {(r["Date"], r["Currency"]): float(r["USDPerUnit"]) for r in load(os.path.join(E1, "fx_rates.csv"))}
RATE = {"USD": 3.75, "EUR": 2.00, "GBP": 3.75, "JPY": 0.50, "INR": 5.50}

out, fails = [], 0


def gate(name, ok, detail=""):
    global fails
    out.append(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    fails += not ok


def fwd(base, ccy, day, mat):
    x = fx[(day, base)] / fx[(day, ccy)]
    tau = (dt.date.fromisoformat(mat) - dt.date.fromisoformat(day)).days
    return x * (1 + RATE[ccy] / 100 * tau / 360) / (1 + RATE[base] / 100 * tau / 360)


def live(k, day):
    return k["TradeDate"] <= day and (k["SettledDate"] == "" or k["SettledDate"] > day)


dates = sorted({h["Date"] for h in hd})
roll_days = sorted({k["TradeDate"] for k in fw if k["Type"] == "Roll"})
by_cls = defaultdict(list)
for k in fw:
    by_cls[k["ClassID"]].append(k)

# G1/G2 notional, ratio, band ---------------------------------------------------------------
b1 = b2 = 0
for h in hd:
    held = sum(float(k["NotionalBase"]) for k in by_cls[h["ClassID"]] if live(k, h["Date"]))
    b1 += abs(held - float(h["HedgeNotionalBase"])) > 0.05
    r = float(h["HedgeNotionalBase"]) / float(h["ClassNAVBase"])
    band = ("HARD BREACH OVER" if r > 1.05 else "HARD BREACH UNDER" if r < 0.95 else
            "REBALANCE DUE" if not (0.97 <= r <= 1.03) else "OK")
    b2 += abs(r - float(h["HedgeRatio"])) > 1e-6 or band != h["BandStatus"] or \
        int(h["IsHardBreach"]) != int(r > 1.05 or r < 0.95)
gate("G1 hedge notional = sum of live forward contracts", b1 == 0, f"{b1} breaks")
gate("G2 hedge ratio and band status recomputed", b2 == 0, f"{b2} breaks")

# G3 rebalance rule and G4 rolls -----------------------------------------------------------
seq = defaultdict(list)
for h in hd:
    seq[h["ClassID"]].append(h)
b3 = b4 = 0
for cid, rows in seq.items():
    for prev, cur in zip(rows, rows[1:]):
        if cur["Date"] in roll_days:
            b4 += abs(float(cur["HedgeNotionalBase"]) - float(prev["ClassNAVBase"])) > 0.05
            continue
        outside = not (0.97 <= float(prev["HedgeRatio"]) <= 1.03)
        b3 += (int(cur["IsRebalanceDay"]) + int(cur["IsMissedRebalance"])) != int(outside)
        if cur["IsRebalanceDay"] == "1":
            b3 += abs(float(cur["HedgeNotionalBase"]) - float(prev["ClassNAVBase"])) > 0.05
gate("G3 every out-of-band close is rebalanced next day to 100%, or flagged as missed", b3 == 0, f"{b3} breaks")
unsettled_matured = [k for k in fw if k["MaturityDate"] <= dates[-1] and k["SettledDate"] == ""]
gate("G4 rolls reset hedge to 100% of last NAV; nothing matured is left unsettled", b4 == 0 and not unsettled_matured,
     f"{b4} roll breaks, {len(unsettled_matured)} unsettled")

# G5 forward pricing, G6 settlement ----------------------------------------------------------
b5 = sum(abs(fwd(k["SellCcy"], k["BuyCcy"], k["TradeDate"], k["MaturityDate"]) - float(k["ForwardRate"]))
         / float(k["ForwardRate"]) > 1e-6 for k in fw)
gate("G5 forward rate = spot x (1 + r class x t/360) / (1 + r base x t/360)", b5 == 0, f"{b5} breaks")
b6 = 0
fund_settle = defaultdict(float)
for k in fw:
    if k["SettledDate"]:
        x = fx[(k["SettledDate"], k["SellCcy"])] / fx[(k["SettledDate"], k["BuyCcy"])]
        pnl = float(k["NotionalBase"]) * (float(k["ForwardRate"]) / x - 1)
        b6 += abs(pnl - float(k["SettlementBase"])) > 0.05
        fund_settle[(k["SettledDate"], k["FundID"])] += float(k["SettlementBase"])
b6 += sum(abs(fund_settle[(r["Date"], r["FundID"])] - float(r["SettlementBase"])) > 0.5 for r in rs)
gate("G6 roll settlement P&L = notional x (contract rate / spot at maturity - 1), summed per fund", b6 == 0,
     f"{b6} breaks")

# G7 mark-to-market per class and per counterparty -----------------------------------------------
b7 = 0
mtm_c = defaultdict(float)
notl = defaultdict(float)
for h in hd:
    m = 0.0
    for k in by_cls[h["ClassID"]]:
        if live(k, h["Date"]):
            v = float(k["NotionalBase"]) * (float(k["ForwardRate"]) / fwd(k["SellCcy"], k["BuyCcy"], h["Date"],
                                                                          k["MaturityDate"]) - 1)
            m += v
            mtm_c[(h["Date"], k["Counterparty"])] += v * fx[(h["Date"], k["SellCcy"])]
            notl[(h["Date"], k["Counterparty"])] += float(k["NotionalBase"]) * fx[(h["Date"], k["SellCcy"])]
    b7 += abs(m - float(h["MTMBase"])) > 0.5
gate("G7 forward mark-to-market recomputed per class", b7 == 0, f"{b7} breaks")

# G8 counterparty exposure --------------------------------------------------------------------
cash = defaultdict(float)
for r in e1:
    cash[(r["Date"], acc[r["AccountID"]]["Bank"])] += max(0.0, float(r["ClosingUSD"]))
b8 = 0
for r in cp:
    key = (r["Date"], r["Counterparty"])
    c_usd = cash[key] if r["CounterpartyType"] == "Custody bank" else 0.0
    expo = c_usd + max(0.0, mtm_c[key])
    util = expo / float(cpty[r["Counterparty"]]["LimitUSD"])
    b8 += abs(c_usd - float(r["CashUSD"])) > 0.05 or abs(mtm_c[key] - float(r["NetMTMUSD"])) > 1.0 \
        or abs(expo - float(r["ExposureUSD"])) > 1.0 or int(r["IsLimitBreach"]) != int(util > 1) \
        or int(r["IsLimitWarning"]) != int(0.9 < util <= 1)
    if r["CounterpartyType"] == "FX counterparty":
        tot = sum(notl[(r["Date"], b)] for b in cpty if cpty[b]["CounterpartyType"] == "FX counterparty")
        b8 += abs(notl[key] / tot - float(r["NotionalShare"])) > 1e-5
        b8 += int(r["IsConcentrationBreach"]) != int(notl[key] / tot > 0.5)
gate("G8 exposure = Engine 1 cash at the bank + positive net forward MTM; limits and 50% share rule",
     b8 == 0, f"{b8} breaks")

# G9 register = flags, episodes consecutive --------------------------------------------------------
days = Counter()
for e in exc:
    days[e["ExceptionType"]] += int(e["Days"])
flags = {
    "HEDGE_RATIO_BREACH": sum(int(h["IsHardBreach"]) for h in hd),
    "MISSED_REBALANCE": sum(int(h["IsMissedRebalance"]) for h in hd),
    "COUNTERPARTY_LIMIT_BREACH": sum(int(r["IsLimitBreach"]) for r in cp),
    "COUNTERPARTY_LIMIT_WARNING": sum(int(r["IsLimitWarning"]) for r in cp),
    "CONCENTRATION_BREACH": sum(int(r["IsConcentrationBreach"]) for r in cp),
}
roll_calls = 0
for r in rs:
    tgt = float(acc[r["BaseAccount"]]["TargetBalance"])
    want = int(float(r["SettlementBase"]) < 0 and -float(r["SettlementBase"]) > tgt)
    roll_calls += want
    flags.setdefault("_bad_roll_flag", 0)
    flags["_bad_roll_flag"] += want != int(r["ExceedsCashBuffer"])
ok9 = all(days[k] == v for k, v in flags.items() if not k.startswith("_")) and flags["_bad_roll_flag"] == 0 \
    and days["ROLL_CASH_CALL"] == roll_calls
gate("G9 exception episodes cover exactly the flagged days (incl. roll cash calls vs Engine 1 buffers)", ok9,
     str({k: v for k, v in flags.items() if not k.startswith("_")}))

# G10 scenarios -------------------------------------------------------------------------------------
for s in scen:
    hit = [e for e in exc if e["Source"] == s["Scenario"] and e["ExceptionType"] == s["ExpectedException"]
           and e["Entity"] == s["Entity"]]
    extra = ""
    if s["Scenario"] == "H2":
        hb = [e for e in exc if e["Source"] == "H2" and e["ExceptionType"] == "HEDGE_RATIO_BREACH"]
        extra = f"; led to hard breach: {bool(hb)}"
    gate(f"G10 {s['Scenario']} caught: {s['ExpectedException']} on {s['Entity']}{extra}", bool(hit),
         f"{hit[0]['StartDate']} to {hit[0]['EndDate']}, peak {hit[0]['Peak']}" if hit else "missing")

# G11 realism --------------------------------------------------------------------------------------
n = len(hd)
hard_nat = sum(int(e["Days"]) for e in exc if e["ExceptionType"] == "HEDGE_RATIO_BREACH" and e["Source"] == "Natural")
reb = sum(int(h["IsRebalanceDay"]) for h in hd)
gate("G11 natural hard breaches rare (<1% of class-days) and rebalancing active", hard_nat / n < 0.01 and reb > 0,
     f"{hard_nat} natural breach days of {n}, {reb} rebalance trades")

facts = [
    f"classes={len(cls)}, class-days={n}, contracts={len(fw)} {dict(Counter(k['Type'] for k in fw))}",
    f"hedged NAV last day USD {sum(float(h['ClassNAVUSD']) for h in hd if h['Date']==dates[-1]):,.0f}",
    f"band status {dict(Counter(h['BandStatus'] for h in hd))}",
    f"exceptions {len(exc)} {dict(Counter(e['ExceptionType'] for e in exc))}",
    f"by source {dict(Counter(e['Source'] for e in exc))}",
    "roll settlements USD m: " + ", ".join(f"{r['Date']} {r['FundID']} {float(r['SettlementUSD'])/1e6:.2f}" for r in rs),
    "max utilization: " + ", ".join(f"{b} {max(float(r['Utilization']) for r in cp if r['Counterparty']==b):.0%}"
                                    for b in cpty),
]
with open(os.path.join(HERE, "verify_report.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(out + [""] + facts) + f"\n\n{len(out) - fails}/{len(out)} gates passed\n")
print(f"{len(out) - fails}/{len(out)} gates passed")
sys.exit(1 if fails else 0)
