"""
Independent checks for Engine 1. Reads ONLY the output CSVs and rebuilds the
numbers from the raw flow book, without importing the engine. A gate that
fails prints FAIL and the script exits non-zero.
Results are written to verify_report.txt (UTF-8).
"""
import csv
import os
import sys
from collections import defaultdict, Counter
import math

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


acct = {a["AccountID"]: a for a in load("dim_account.csv")}
flows = load("cash_flows.csv")
daily = load("daily_position.csv")
ladder = load("cash_ladder.csv")
exc = load("exceptions.csv")
scen = load("_scenarios.csv")
fxr = {(r["Date"], r["Currency"]): float(r["USDPerUnit"]) for r in load("fx_rates.csv")}

out = []
fails = 0


def gate(name, ok, detail=""):
    global fails
    out.append(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        fails += 1


def xround(x, nd=-3):
    q = 10 ** (-nd)
    return int(math.copysign(math.floor(abs(x) / q + 0.5) * q, x)) if x else 0


dates = sorted({r["Date"] for r in daily})
fc = defaultdict(int)       # (acct, valuedate, knowndate) -> forecast
act = defaultdict(int)
sweep = defaultdict(int)
for f in flows:
    if f["FlowType"] == "SWEEP":
        sweep[(f["AccountID"], f["ActualValueDate"])] += int(f["ActualAmount"])
        continue
    if f["ActualValueDate"]:
        act[(f["AccountID"], f["ActualValueDate"])] += int(f["ActualAmount"])


def forecast(aid, vd, asof):
    return sum(int(f["ForecastAmount"]) for f in by_fvd[(aid, vd)] if f["KnownDate"] <= asof)


by_fvd = defaultdict(list)
for f in flows:
    if f["FlowType"] != "SWEEP":
        by_fvd[(f["AccountID"], f["ForecastValueDate"])].append(f)

# G1 balance roll and tie-out -----------------------------------------------------------
bad_roll = bad_close = bad_act = bad_sweep = 0
prev = {}
for r in daily:
    aid, d = r["AccountID"], r["Date"]
    o, c = int(r["Opening"]), int(r["Closing"])
    expected_open = prev.get(aid, int(acct[aid]["TargetBalance"]))
    bad_roll += o != expected_open
    bad_close += c != o + int(r["TreasuryAction"]) + int(r["ActualNetFlow"])
    bad_act += int(r["ActualNetFlow"]) != act[(aid, d)]
    bad_sweep += int(r["TreasuryAction"]) != sweep[(aid, d)]
    prev[aid] = c
gate("G1a opening(T) = closing(T-1), day 1 = target", bad_roll == 0, f"{bad_roll} breaks")
gate("G1b closing = opening + action + actual flows", bad_close == 0, f"{bad_close} breaks")
gate("G1c actual net flow = sum of settled flows in cash_flows.csv", bad_act == 0, f"{bad_act} breaks")
gate("G1d treasury action = sweep flows booked", bad_sweep == 0, f"{bad_sweep} breaks")

# G2 re-implement forecast + sweep rule ----------------------------------------------------
bad_fc = bad_rule = 0
for r in daily:
    aid, d = r["AccountID"], r["Date"]
    a = acct[aid]
    f0 = forecast(aid, d, d)
    bad_fc += f0 != int(r["ForecastNetFlow"])
    pre = int(r["Opening"]) + f0
    if pre < int(a["MinBalance"]) or pre > int(a["IdleThreshold"]):
        want = xround(int(a["TargetBalance"]) - pre)
    else:
        want = 0
    bad_rule += want != int(r["TreasuryAction"])
gate("G2a T+0 forecast = flows known by the morning", bad_fc == 0, f"{bad_fc} breaks")
gate("G2b sweep rule reproduces every treasury action", bad_rule == 0, f"{bad_rule} breaks")

# G3 ladder --------------------------------------------------------------------------------
open_of = {(r["AccountID"], r["Date"]): int(r["Opening"]) for r in daily}
bad_l0 = bad_cons = bad_lfc = 0
prevc = {}
for r in ladder:
    key = (r["AccountID"], r["AsOfDate"])
    h = int(r["Horizon"])
    if h == 0:
        bad_l0 += int(r["ProjOpening"]) != open_of[key]
    else:
        bad_cons += int(r["ProjOpening"]) != prevc[key]
    bad_cons += int(r["ProjClosing"]) != int(r["ProjOpening"]) + int(r["ProjNetFlow"]) + int(r["TreasuryAction"])
    bad_lfc += int(r["ProjNetFlow"]) != forecast(r["AccountID"], r["ValueDate"], r["AsOfDate"])
    prevc[key] = int(r["ProjClosing"])
gate("G3a ladder T+0 opening = actual opening", bad_l0 == 0, f"{bad_l0} breaks")
gate("G3b ladder rolls: closing(h) = opening(h) + flows + action, opening(h) = closing(h-1)", bad_cons == 0,
     f"{bad_cons} breaks")
gate("G3c ladder flows use only information known at as-of date", bad_lfc == 0, f"{bad_lfc} breaks")

# G4 injected scenarios caught, on the right account and date --------------------------------
for s in scen:
    exp = [e for e in s["ExpectedExceptions"].split("|") if e]
    got = {(e["ExceptionType"]) for e in exc if e["Source"] == s["Scenario"]}
    if s["Scenario"] == "S4":
        on_day = {e["ExceptionType"] for e in exc if e["Source"] == "S4"}
    else:
        on_day = {e["ExceptionType"] for e in exc if e["AccountID"] == s["AccountID"] and e["Date"] == s["Date"]}
    ok = set(exp) <= got and set(exp) <= on_day | ({"DUPLICATE_FLOW"} if s["Scenario"] == "S4" else set())
    if s["Scenario"] == "S7":
        warn = [r for r in ladder if r["AccountID"] == s["AccountID"] and r["ValueDate"] == s["Date"]
                and int(r["Horizon"]) > 0]
        warned = [r for r in warn if r["LadderFlag"] == "FUNDING NEED"]
        closing = [int(r["Closing"]) for r in daily if r["AccountID"] == s["AccountID"] and r["Date"] == s["Date"]][0]
        no_exc = not any(e for e in exc if e["AccountID"] == s["AccountID"] and e["Date"] == s["Date"])
        ok = len(warned) == len(warn) == 5 and closing >= 0 and no_exc
        gate(f"G4 {s['Scenario']} early warning: flagged on all {len(warn)} prior ladders, no overdraft on the day",
             ok, f"warned {len(warned)}/{len(warn)}, closing {closing:,}")
    else:
        gate(f"G4 {s['Scenario']} caught: expected {exp}", ok, f"got {sorted(got)}")

# G5 every exception traced to a real flow ------------------------------------------------
fid = {f["FlowID"]: f for f in flows}
untraced = [e["ExceptionID"] for e in exc if not e["RootCause"] or e["RootFlowID"] not in fid]
gate("G5 every exception has a root cause and a real flow behind it", not untraced, f"untraced {untraced}")
nat = [e for e in exc if e["Source"] == "Natural"]
nat_ok = all(fid[e["RootFlowID"]]["Status"] in ("FAILED", "UNFORECAST", "SETTLED", "DUPLICATE") for e in nat)
gate("G5b natural exceptions come from genuine fails / late notices", nat_ok,
     str(Counter(e["RootCause"] for e in nat)))

# G6 register is complete vs the daily flags ------------------------------------------------
cnt = Counter(e["ExceptionType"] for e in exc)
gate("G6 register matches flags", cnt["ACTUAL_OVERDRAFT"] == sum(int(r["IsOverdraft"]) for r in daily)
     and cnt["IDLE_CASH"] == sum(int(r["IsIdle"]) for r in daily)
     and cnt["FORECAST_VARIANCE"] == sum(int(r["IsVarianceBreach"]) for r in daily), str(dict(cnt)))

# G7 forecast accuracy shape --------------------------------------------------------------
err, gross = defaultdict(float), defaultdict(float)
for r in ladder:
    if r["HasActual"] == "1":
        err[int(r["Horizon"])] += float(r["AbsErrorUSD"])
        gross[int(r["Horizon"])] += float(r["ActualGrossUSD"])
accs = [1 - err[h] / gross[h] for h in range(6)]
gate("G7 accuracy below 100% and decays with horizon", accs[0] < 0.995 and accs[0] >= accs[2] >= accs[5],
     " ".join(f"T+{h}={a:.1%}" for h, a in enumerate(accs)))

# G8 liquidity fund never exhausted ---------------------------------------------------------
gate("G8 MMF balance never negative", min(int(r["MMFBalance"]) for r in daily) > 0)

# G9 USD conversion uses the day's FX rate ----------------------------------------------------
bad_fx = sum(abs(float(r["ClosingUSD"]) - int(r["Closing"]) * fxr[(r["Date"], r["Currency"])]) > 0.01 for r in daily)
gate("G9 USD values = local x FX of the day", bad_fx == 0, f"{bad_fx} breaks")

# G10 duplicate detection rebuilt independently --------------------------------------------
k = Counter((f["AccountID"], f["FlowType"], f["Counterparty"], f["ForecastValueDate"], f["ForecastAmount"])
            for f in flows if f["FlowType"] != "SWEEP" and f["ForecastAmount"] != "0")
dups = sum(v - 1 for v in k.values() if v > 1)
gate("G10 duplicate check agrees with register", dups == cnt["DUPLICATE_FLOW"], f"{dups} duplicates")

# summary facts for the guide
last = dates[-1]
facts = [
    f"days={len(dates)} ({dates[0]} to {last}), accounts={len(acct)}, flows={len(flows)}",
    f"exceptions={len(exc)} by type {dict(cnt)}",
    f"by source {dict(Counter(e['Source'] for e in exc))}",
    f"cash last day USD {sum(float(r['ClosingUSD']) for r in daily if r['Date'] == last):,.0f}",
    f"overdraft cost USD {sum(float(r['OverdraftCostUSD']) for r in daily):,.0f}, "
    f"idle drag USD {sum(float(r['IdleDragUSD']) for r in daily):,.0f}",
    f"sweeps {dict(Counter(r['ActionType'] for r in daily))}",
    f"ladder flags {dict(Counter(r['LadderFlag'] for r in ladder))}",
    f"latest as-of funding needs {sum(1 for r in ladder if r['AsOfDate']==last and r['LadderFlag']=='FUNDING NEED')}",
]
with open(os.path.join(HERE, "verify_report.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(out + [""] + facts) + "\n")
    fh.write(f"\n{len(out) - fails}/{len(out)} gates passed\n")
print(f"{len(out) - fails}/{len(out)} gates passed")
sys.exit(1 if fails else 0)
