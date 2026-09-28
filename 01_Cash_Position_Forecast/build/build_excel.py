"""
Builds Cash_Position_Forecast_Model.xlsx from the engine CSVs.
Daily Position and Ladder are live formulas (SUMIFS on the flow book), so the
workbook re-derives the engine instead of just displaying it. Only functions
available in Excel 2010+ are used (SUMIFS, INDEX/MATCH, WORKDAY, ROUND).
"""
import csv
import datetime as dt
import os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.chart import BarChart, Reference

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
XLSX = os.path.join(HERE, "..", "Cash_Position_Forecast_Model.xlsx")

NAVY = "1F3864"
HDR_FILL = PatternFill("solid", fgColor=NAVY)
HDR_FONT = Font(bold=True, color="FFFFFF")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")
RED_FILL = PatternFill("solid", fgColor="F8CBAD")
AMBER_FILL = PatternFill("solid", fgColor="FFE699")
GREEN_FILL = PatternFill("solid", fgColor="C6EFCE")
NUM = "#,##0;[Red]-#,##0"
DATE = "yyyy-mm-dd"


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def d(s):
    return dt.date.fromisoformat(s) if s else None


def header(ws, cols, row=1, freeze=True):
    for j, c in enumerate(cols, 1):
        cell = ws.cell(row=row, column=j, value=c)
        cell.fill, cell.font = HDR_FILL, HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    if freeze:
        ws.freeze_panes = ws.cell(row=row + 1, column=1)


def widths(ws, w):
    for j, x in enumerate(w, 1):
        ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = x


def name(wb, n, ref):
    wb.defined_names[n] = DefinedName(n, attr_text=ref)


acct = load("dim_account.csv")
flows = load("cash_flows.csv")
daily = load("daily_position.csv")
exc = load("exceptions.csv")
scen = load("_scenarios.csv")
fx = load("fx_rates.csv")

wb = Workbook()

# ------------------------------------------------------------------ About
ws = wb.active
ws.title = "About"
lines = [
    ("Cash Position and Forecast Ladder", True),
    ("Treasury Operations Analytics, Engine 1 (synthetic data, 6 funds, 15 bank accounts, 60 business days)", False),
    ("", False),
    ("How the desk uses it, each business day:", True),
    ("1. Morning: start from the bank's closing balance of yesterday (Opening).", False),
    ("2. Project today and the next 5 days from every flow already known (Ladder sheet).", False),
    ("3. Act on today: fund from the money market fund if below the buffer, sweep to it if above the idle threshold.", False),
    ("4. Evening: book the actual flows from the bank statement, compare with the forecast, raise exceptions.", False),
    ("", False),
    ("Sheets:", True),
    ("Accounts: policy per account (minimum buffer, target, idle threshold, rates). Yellow cell = editable tolerance.", False),
    ("Flows: the flow book. Forecast side (known date, value date, amount) and actual side (bank statement).", False),
    ("Daily Position: live formulas. Opening, forecast, treasury action, actual, closing, flags, cost.", False),
    ("Ladder: pick an as-of date and an account (yellow cells) to see the T to T+5 cash ladder.", False),
    ("Exceptions: register with root cause traced to the individual flow.", False),
    ("Checks: tie-outs that must all say PASS.", False),
    ("", False),
    ("Injected test scenarios (the checks prove each one is caught):", True),
]
for s in scen:
    lines.append((f"{s['Scenario']}  {s['Date']}  {s['AccountID']}  {s['Description']}", False))
lines += [
    ("", False),
    ("Calendar is weekdays only (public holidays not modelled). Rates are illustrative. All data is synthetic.", False),
]
for i, (t, b) in enumerate(lines, 1):
    c = ws.cell(row=i, column=1, value=t)
    c.font = Font(bold=b, size=14 if i == 1 else 11, color=NAVY if b else "000000")
ws.column_dimensions["A"].width = 120

# ------------------------------------------------------------------ Accounts
ws = wb.create_sheet("Accounts")
acols = ["AccountID", "FundID", "FundName", "FundBaseCcy", "Currency", "Bank", "AccountType", "MinBalance",
         "TargetBalance", "IdleThreshold", "VarianceFloor", "OverdraftRatePct", "MMFYieldPct", "DayBasis",
         "MMFOpening"]
header(ws, acols)
for i, a in enumerate(acct, 2):
    for j, c in enumerate(acols, 1):
        v = a[c]
        if c in ("MinBalance", "TargetBalance", "IdleThreshold", "VarianceFloor", "DayBasis", "MMFOpening"):
            v = int(v)
        elif c in ("OverdraftRatePct", "MMFYieldPct"):
            v = float(v)
        cell = ws.cell(row=i, column=j, value=v)
        if isinstance(v, int) and c != "DayBasis":
            cell.number_format = NUM
widths(ws, [10, 8, 28, 10, 9, 9, 17, 16, 16, 16, 15, 11, 11, 9, 17])
n_acc = len(acct) + 1
ws["Q1"], ws["R1"] = "Assumption", "Value"
ws["Q1"].fill = ws["R1"].fill = HDR_FILL
ws["Q1"].font = ws["R1"].font = HDR_FONT
ws["Q2"] = "Variance tolerance, % of gross flows"
ws["R2"] = 0.05
ws["R2"].number_format = "0.0%"
ws["R2"].fill = INPUT_FILL
ws.column_dimensions["Q"].width = 36
for n, col in [("Acc_ID", "A"), ("Acc_Fund", "C"), ("Acc_Ccy", "E"), ("Acc_Bank", "F"), ("Acc_Min", "H"),
               ("Acc_Target", "I"), ("Acc_Idle", "J"), ("Acc_Floor", "K"), ("Acc_OD", "L"), ("Acc_Yield", "M"),
               ("Acc_Basis", "N")]:
    name(wb, n, f"Accounts!${col}$2:${col}${n_acc}")
name(wb, "TolPct", "Accounts!$R$2")

# ------------------------------------------------------------------ FX
ws = wb.create_sheet("FX")
ccys = ["USD", "EUR", "GBP", "JPY", "INR"]
header(ws, ["Date"] + ccys)
fxd = {}
for r in fx:
    fxd.setdefault(r["Date"], {})[r["Currency"]] = float(r["USDPerUnit"])
for i, day in enumerate(sorted(fxd), 2):
    ws.cell(row=i, column=1, value=d(day)).number_format = DATE
    for j, c in enumerate(ccys, 2):
        ws.cell(row=i, column=j, value=fxd[day][c]).number_format = "0.000000" if c != "JPY" else "0.00000000"
n_fx = len(fxd) + 1
name(wb, "FX_Dates", f"FX!$A$2:$A${n_fx}")
name(wb, "FX_Table", f"FX!$B$2:$F${n_fx}")
name(wb, "FX_Ccy", "FX!$B$1:$F$1")
widths(ws, [12, 10, 10, 10, 12, 10])

# ------------------------------------------------------------------ Flows
ws = wb.create_sheet("Flows")
fcols = ["FlowID", "AccountID", "FlowType", "Counterparty", "KnownDate", "ForecastValueDate", "ForecastAmount",
         "ActualValueDate", "ActualAmount", "Status", "Scenario", "Note"]
header(ws, fcols)
for i, f in enumerate(flows, 2):
    row = [f["FlowID"], f["AccountID"], f["FlowType"], f["Counterparty"], d(f["KnownDate"]),
           d(f["ForecastValueDate"]), int(f["ForecastAmount"]), d(f["ActualValueDate"]), int(f["ActualAmount"]),
           f["Status"], f["Scenario"], f["Note"]]
    for j, v in enumerate(row, 1):
        c = ws.cell(row=i, column=j, value=v)
        if j in (5, 6, 8):
            c.number_format = DATE
        elif j in (7, 9):
            c.number_format = NUM
n_fl = len(flows) + 1
for n, col in [("F_Acct", "B"), ("F_Type", "C"), ("F_Known", "E"), ("F_FVD", "F"), ("F_FAmt", "G"),
               ("F_AVD", "H"), ("F_AAmt", "I")]:
    name(wb, n, f"Flows!${col}$2:${col}${n_fl}")
widths(ws, [9, 10, 15, 28, 12, 13, 17, 13, 17, 12, 10, 40])
ws.auto_filter.ref = f"A1:L{n_fl}"

# ------------------------------------------------------------------ Daily Position (live formulas)
ws = wb.create_sheet("Daily Position")
dcols = ["Date", "AccountID", "Currency", "Opening", "Forecast Inflows", "Forecast Outflows", "Forecast Net",
         "Pre-action Projected", "Treasury Action", "Action Type", "Actual Inflows", "Actual Outflows",
         "Actual Net", "Gross Flows", "Variance (Actual - Forecast)", "Closing", "Tolerance", "Overdraft",
         "Idle", "Variance Breach", "Status", "FX Rate", "Closing USD", "Overdraft Cost USD", "Idle Drag USD",
         "Key"]
header(ws, dcols)
rows = sorted(daily, key=lambda r: (r["AccountID"], r["Date"]))
for i, r in enumerate(rows, 2):
    m = f"MATCH(B{i},Acc_ID,0)"
    ws.cell(row=i, column=1, value=d(r["Date"])).number_format = DATE
    ws.cell(row=i, column=2, value=r["AccountID"])
    f = {
        3: f"=INDEX(Acc_Ccy,{m})",
        4: f"=IF(B{i}<>B{i-1},INDEX(Acc_Target,{m}),P{i-1})",
        5: f'=SUMIFS(F_FAmt,F_Acct,B{i},F_FVD,A{i},F_Known,"<="&A{i},F_Type,"<>SWEEP",F_FAmt,">0")',
        6: f'=SUMIFS(F_FAmt,F_Acct,B{i},F_FVD,A{i},F_Known,"<="&A{i},F_Type,"<>SWEEP",F_FAmt,"<0")',
        7: f"=E{i}+F{i}",
        8: f"=D{i}+G{i}",
        9: f"=IF(OR(H{i}<INDEX(Acc_Min,{m}),H{i}>INDEX(Acc_Idle,{m})),ROUND(INDEX(Acc_Target,{m})-H{i},-3),0)",
        10: f'=IF(I{i}>0,"FUND FROM MMF",IF(I{i}<0,"SWEEP TO MMF","NONE"))',
        11: f'=SUMIFS(F_AAmt,F_Acct,B{i},F_AVD,A{i},F_Type,"<>SWEEP",F_AAmt,">0")',
        12: f'=SUMIFS(F_AAmt,F_Acct,B{i},F_AVD,A{i},F_Type,"<>SWEEP",F_AAmt,"<0")',
        13: f"=K{i}+L{i}",
        14: f"=K{i}-L{i}",
        15: f"=M{i}-G{i}",
        16: f"=D{i}+I{i}+M{i}",
        17: f"=MAX(INDEX(Acc_Floor,{m}),ROUND(TolPct*N{i},0))",
        18: f"=IF(P{i}<0,1,0)",
        19: f"=IF(P{i}>INDEX(Acc_Idle,{m}),1,0)",
        20: f"=IF(ABS(O{i})>Q{i},1,0)",
        21: f'=IF(R{i}+S{i}+T{i}>0,"EXCEPTION","OK")',
        22: f"=INDEX(FX_Table,MATCH(A{i},FX_Dates,0),MATCH(C{i},FX_Ccy,0))",
        23: f"=P{i}*V{i}",
        24: f"=MAX(0,-P{i})*INDEX(Acc_OD,{m})/100/INDEX(Acc_Basis,{m})*V{i}",
        25: f"=MAX(0,P{i}-INDEX(Acc_Target,{m}))*INDEX(Acc_Yield,{m})/100/INDEX(Acc_Basis,{m})*V{i}",
        26: f'=B{i}&"|"&A{i}',
    }
    for j, v in f.items():
        c = ws.cell(row=i, column=j, value=v)
        if j in (4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 23, 24, 25):
            c.number_format = NUM
        elif j == 22:
            c.number_format = "0.000000"
n_dp = len(rows) + 1
for n, col in [("DP_Date", "A"), ("DP_Opening", "D"), ("DP_Action", "I"), ("DP_Closing", "P"), ("DP_Key", "Z"),
               ("DP_Net", "M")]:
    name(wb, n, f"'Daily Position'!${col}${2}:${col}${n_dp}")
widths(ws, [11, 9, 8] + [15] * 6 + [14] + [15] * 7 + [14, 9, 6, 8, 11, 10, 15, 12, 12, 14])
ws.conditional_formatting.add(f"U2:U{n_dp}", CellIsRule(operator="equal", formula=['"EXCEPTION"'], fill=RED_FILL))
ws.conditional_formatting.add(f"P2:P{n_dp}", CellIsRule(operator="lessThan", formula=["0"], fill=RED_FILL))
ws.auto_filter.ref = f"A1:Z{n_dp}"

# ------------------------------------------------------------------ Ladder (interactive)
ws = wb.create_sheet("Ladder")
last_date = max(r["Date"] for r in daily)
ws["A1"] = "Cash Ladder: T to T+5"
ws["A1"].font = Font(bold=True, size=14, color=NAVY)
ws["A2"], ws["B2"] = "As-of date", d(last_date)
ws["A3"], ws["B3"] = "Account", "A05"
for c in ("B2", "B3"):
    ws[c].fill = INPUT_FILL
    ws[c].font = Font(bold=True)
ws["B2"].number_format = DATE
ws["C2"] = f"(any business day {min(r['Date'] for r in daily)} to {last_date})"
dv = DataValidation(type="list", formula1=f"Accounts!$A$2:$A${n_acc}", allow_blank=False)
ws.add_data_validation(dv)
dv.add("B3")
ws["D3"] = '=INDEX(Acc_Fund,MATCH(B3,Acc_ID,0))&"  |  "&INDEX(Acc_Ccy,MATCH(B3,Acc_ID,0))&"  |  "&INDEX(Acc_Bank,MATCH(B3,Acc_ID,0))'
ws["A4"], ws["B4"] = "Min buffer", "=INDEX(Acc_Min,MATCH(B3,Acc_ID,0))"
ws["C4"], ws["D4"] = "Target", "=INDEX(Acc_Target,MATCH(B3,Acc_ID,0))"
ws["E4"], ws["F4"] = "Idle threshold", "=INDEX(Acc_Idle,MATCH(B3,Acc_ID,0))"
for c in ("B4", "D4", "F4"):
    ws[c].number_format = NUM
lcols = ["Horizon", "Value Date", "Projected Opening", "Known Inflows", "Known Outflows", "Net Known Flows",
         "Treasury Action", "Projected Closing", "Ladder Flag", "Actual Net Flow", "Forecast Error"]
header(ws, lcols, row=6, freeze=False)
for h in range(6):
    i = 7 + h
    ws.cell(row=i, column=1, value=f"T+{h}")
    ws.cell(row=i, column=2, value=f"=WORKDAY($B$2,{h})").number_format = DATE
    ws.cell(row=i, column=3, value=('=INDEX(DP_Opening,MATCH($B$3&"|"&$B$2,DP_Key,0))' if h == 0 else f"=H{i-1}"))
    ws.cell(row=i, column=4,
            value=f'=SUMIFS(F_FAmt,F_Acct,$B$3,F_FVD,B{i},F_Known,"<="&$B$2,F_Type,"<>SWEEP",F_FAmt,">0")')
    ws.cell(row=i, column=5,
            value=f'=SUMIFS(F_FAmt,F_Acct,$B$3,F_FVD,B{i},F_Known,"<="&$B$2,F_Type,"<>SWEEP",F_FAmt,"<0")')
    ws.cell(row=i, column=6, value=f"=D{i}+E{i}")
    ws.cell(row=i, column=7, value=('=INDEX(DP_Action,MATCH($B$3&"|"&$B$2,DP_Key,0))' if h == 0 else 0))
    ws.cell(row=i, column=8, value=f"=C{i}+F{i}+G{i}")
    if h == 0:
        flag = f'=IF(G{i}>0,"ACTION: FUND FROM MMF",IF(G{i}<0,"ACTION: SWEEP TO MMF","OK"))'
    else:
        flag = f'=IF(H{i}<$B$4,"FUNDING NEED",IF(H{i}>$F$4,"SURPLUS TO SWEEP","OK"))'
    ws.cell(row=i, column=9, value=flag)
    ws.cell(row=i, column=10,
            value=f'=IF(B{i}<=MAX(DP_Date),SUMIFS(F_AAmt,F_Acct,$B$3,F_AVD,B{i},F_Type,"<>SWEEP"),"not yet")')
    ws.cell(row=i, column=11, value=f'=IF(ISNUMBER(J{i}),J{i}-F{i},"")')
    for j in (3, 4, 5, 6, 7, 8, 10, 11):
        ws.cell(row=i, column=j).number_format = NUM
ws.conditional_formatting.add("I7:I12", FormulaRule(formula=['LEFT(I7,7)="FUNDING"'], fill=RED_FILL))
ws.conditional_formatting.add("I7:I12", FormulaRule(formula=['LEFT(I7,7)="SURPLUS"'], fill=AMBER_FILL))
ws.conditional_formatting.add("I7:I12", FormulaRule(formula=['LEFT(I7,6)="ACTION"'], fill=GREEN_FILL))
widths(ws, [14, 12, 17, 15, 15, 15, 15, 17, 24, 15, 15])
ws["A14"] = ("Reading it: rows T+1 to T+5 use only flows known on the as-of date, so they are a funding plan. "
             "Accuracy falls after T+2 because trades not yet executed cannot be in the ladder.")
ws["A15"] = "Try: as-of 2026-08-03, account A05 (scenario S7, a large redemption notified 5 days ahead)."
ch = BarChart()
ch.title = "Projected closing balance"
ch.y_axis.title = "Local currency"
ch.add_data(Reference(ws, min_col=8, min_row=6, max_row=12), titles_from_data=True)
ch.set_categories(Reference(ws, min_col=1, min_row=7, max_row=12))
ch.height, ch.width = 8, 16
ch.legend = None
ws.add_chart(ch, "A17")

# ------------------------------------------------------------------ Exceptions (register values)
ws = wb.create_sheet("Exceptions")
ecols = ["ExceptionID", "Date", "AccountID", "FundName", "Currency", "Bank", "ExceptionType", "Severity",
         "AmountLocal", "AmountUSD", "RootCause", "RootFlowID", "Source", "SuggestedAction"]
header(ws, ecols)
for i, e in enumerate(exc, 2):
    for j, c in enumerate(ecols, 1):
        v = e[c]
        if c == "Date":
            v = d(v)
        elif c in ("AmountLocal", "AmountUSD"):
            v = float(v)
        cell = ws.cell(row=i, column=j, value=v)
        if c == "Date":
            cell.number_format = DATE
        elif c in ("AmountLocal", "AmountUSD"):
            cell.number_format = NUM
n_ex = len(exc) + 1
name(wb, "Exc_Type", f"Exceptions!$G$2:$G${n_ex}")
widths(ws, [10, 11, 9, 26, 8, 8, 20, 9, 17, 15, 24, 10, 9, 52])
ws.conditional_formatting.add(f"H2:H{n_ex}", CellIsRule(operator="equal", formula=['"High"'], fill=RED_FILL))
ws.auto_filter.ref = f"A1:N{n_ex}"

# ------------------------------------------------------------------ Checks
ws = wb.create_sheet("Checks")
header(ws, ["Check", "Left", "Right", "Result"])
dp = "'Daily Position'"
last = f'MAX(DP_Date)'
checks = [
    ("Overdraft flags = register ACTUAL_OVERDRAFT", f"=SUM({dp}!R2:R{n_dp})", '=COUNTIF(Exc_Type,"ACTUAL_OVERDRAFT")'),
    ("Idle flags = register IDLE_CASH", f"=SUM({dp}!S2:S{n_dp})", '=COUNTIF(Exc_Type,"IDLE_CASH")'),
    ("Variance flags = register FORECAST_VARIANCE", f"=SUM({dp}!T2:T{n_dp})",
     '=COUNTIF(Exc_Type,"FORECAST_VARIANCE")'),
    ("Sum of daily actual net = settled flows in book (ex sweeps)", "=SUM(DP_Net)",
     f'=SUMIFS(F_AAmt,F_Type,"<>SWEEP",F_AVD,"<="&{last})'),
    ("Sum of treasury actions = sweep flows in book", "=SUM(DP_Action)", '=SUMIFS(F_AAmt,F_Type,"SWEEP")'),
    ("Last-day closing = targets + all actions + all actual flows (local ccy summed)",
     f'=SUMIFS(DP_Closing,DP_Date,{last})', "=SUM(Acc_Target)+SUM(DP_Action)+SUM(DP_Net)"),
]
for i, (label, left, right) in enumerate(checks, 2):
    ws.cell(row=i, column=1, value=label)
    ws.cell(row=i, column=2, value=left).number_format = NUM
    ws.cell(row=i, column=3, value=right).number_format = NUM
    ws.cell(row=i, column=4, value=f'=IF(ROUND(B{i}-C{i},0)=0,"PASS","FAIL")')
ws.conditional_formatting.add("D2:D20", CellIsRule(operator="equal", formula=['"PASS"'], fill=GREEN_FILL))
ws.conditional_formatting.add("D2:D20", CellIsRule(operator="equal", formula=['"FAIL"'], fill=RED_FILL))
widths(ws, [70, 20, 20, 10])

wb.calculation.fullCalcOnLoad = True
wb.save(XLSX)
print("saved", XLSX)
