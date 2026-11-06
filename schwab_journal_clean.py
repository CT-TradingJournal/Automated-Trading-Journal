import csv
import datetime
import glob
import json
import os
import re
import webbrowser
import pandas as pd

# ================= 帳戶基礎設定 =================
DEFAULT_START_EQUITY = 10000.0        # 蒙地卡羅模擬器預設起始資金 ($)
CHALLENGE_START_DATE = "2024-01-01"   # 90日挑戰起始日 (避免算入歷史舊單)
ADD_LOSER_BUFFER = 0.02               # 逆勢加倉判定門檻
EXCLUDE_SYMBOLS = {"BIL", "SPY"}      # 排除非日內交易標的 (請自行替換為你的長線持有標的)
USE_US_SESSION_DATE = True            # 香港凌晨平倉自動歸入同一個美股交易日
# ===============================================

IGNORE_CSVS = {"sample_trades.csv", "audit_report.csv", "tos_demo_statement.csv", "journal_closed_trades.csv"}
CASH_TRD_PATTERN = re.compile(r"^(BOT|SOLD)\s+([+-]?[\d,]+)\s+([A-Z0-9./]+)\s+@([\d.]+)(?:\s+[A-Z]+)?$")

PREMARKET_DB_FILE = "premarket_watchlist_db.json"
def load_premarket_db():
    if os.path.exists(PREMARKET_DB_FILE):
        try:
            with open(PREMARKET_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def clean_num(val):
    if val is None: return 0.0
    s = str(val).replace("$", "").replace(",", "").replace("+", "").strip()
    if not s: return 0.0
    if s.startswith("(") and s.endswith(")"): s = "-" + s[1:-1]
    return float(s)

def hkt_to_us_eastern(dt_obj, is_hkt):
    if not is_hkt: return dt_obj
    utc_dt = dt_obj - datetime.timedelta(hours=8)
    y = utc_dt.year
    mar1 = datetime.datetime(y, 3, 1)
    dst_start = mar1 + datetime.timedelta(days=(6 - mar1.weekday()) % 7 + 7, hours=7)
    nov1 = datetime.datetime(y, 11, 1)
    dst_end = nov1 + datetime.timedelta(days=(6 - nov1.weekday()) % 7, hours=6)
    offset = -4 if (dst_start <= utc_dt < dst_end) else -5
    return utc_dt + datetime.timedelta(hours=offset)

def classify_us_session(et_dt):
    hm = et_dt.hour * 60 + et_dt.minute
    if hm < 570: return "1. Pre-Market (<09:30)"
    if hm < 600: return "2. Open 30m (09:30-10:00)"
    if hm < 630: return "3. Morning (10:00-10:30)"
    if hm < 660: return "4. Late Morn (10:30-11:00)"
    if hm < 720: return "5. Midday (11:00-12:00)"
    return "6. Afternoon (12:00+)"

def parse_tos_cash_balance(filepath):
    fills = []
    with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.reader(f)
        in_cash_section, col_idx, occ_tracker, seq_no = False, {}, {}, 0
        for row in reader:
            stripped = [c.strip() for c in row]
            if not any(stripped): continue
            if stripped[0] in ("Account Order History", "Account Trade History", "Equities", "Profits and Losses"):
                if in_cash_section: break
            if stripped[0] == "Cash Balance" and all(c == "" for c in stripped[1:]):
                in_cash_section = True; continue
            if in_cash_section:
                lower_row = [c.lower() for c in stripped]
                if "date" in lower_row and "description" in lower_row and "type" in lower_row:
                    col_idx = {name: i for i, name in enumerate(lower_row) if name}
                    continue
                if col_idx and len(stripped) > col_idx.get("description", 4):
                    if stripped[0].lower().startswith("total"): break
                    if stripped[col_idx["type"]].upper() != "TRD": continue
                    desc = stripped[col_idx["description"]]
                    upper_desc = desc.upper()
                    if any(k in upper_desc for k in (" CALL ", " PUT ", "(WEEKLYS)", "CUSTOM")): continue
                    m = CASH_TRD_PATTERN.match(desc)
                    if not m: continue
                    action, qty_str, sym_str, price_str = m.groups()
                    sym_str = sym_str.upper()
                    if sym_str in EXCLUDE_SYMBOLS: continue
                    date_str, time_str = stripped[col_idx["date"]], stripped[col_idx["time"]]
                    ref_raw = stripped[col_idx.get("ref #", 3)].replace('="', "").replace('"', "").strip()
                    misc_fee = abs(clean_num(stripped[col_idx["misc fees"]])) if "misc fees" in col_idx else 0.0
                    comm_fee = abs(clean_num(stripped[col_idx["commissions & fees"]])) if "commissions & fees" in col_idx else 0.0
                    dt = pd.to_datetime(f"{date_str} {time_str}").to_pydatetime()
                    qty = abs(clean_num(qty_str))
                    price = float("0" + price_str) if price_str.startswith(".") else float(price_str)
                    side_sign = 1 if action == "BOT" else -1
                    sig_base = (dt.strftime("%Y-%m-%d %H:%M:%S"), ref_raw, sym_str, side_sign, qty, round(price, 6))
                    occ_tracker[sig_base] = occ_tracker.get(sig_base, 0) + 1
                    seq_no += 1
                    fills.append({
                        "dedup_key": sig_base + (occ_tracker[sig_base],),
                        "datetime": dt, "seq": seq_no, "symbol": sym_str,
                        "side_sign": side_sign, "qty": qty, "price": price, "fees": misc_fee + comm_fee
                    })
    return fills

def parse_tos_trade_history_fallback(filepath):
    raw_rows = []
    with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.reader(f)
        in_trade_section, col_idx = False, {}
        for row in reader:
            stripped = [c.strip() for c in row]
            if not any(stripped):
                if in_trade_section and col_idx: break
                continue
            lower_row = [c.lower() for c in stripped]
            if "exec time" in lower_row and "side" in lower_row and "symbol" in lower_row:
                in_trade_section = True; col_idx = {name: i for i, name in enumerate(lower_row) if name}; continue
            if in_trade_section and col_idx:
                try:
                    type_str = stripped[col_idx["type"]].upper() if "type" in col_idx else "STOCK"
                    if type_str in ("CALL", "PUT", "FUTURE", "FUTURES"): continue
                    sym_str = stripped[col_idx["symbol"]].upper()
                    if not sym_str or sym_str in EXCLUDE_SYMBOLS: continue
                    dt = pd.to_datetime(stripped[col_idx["exec time"]]).to_pydatetime()
                    side_str = stripped[col_idx["side"]].upper()
                    side_sign = 1 if ("BUY" in side_str or "BOT" in side_str or "COVER" in side_str) else -1
                    qty, price = abs(clean_num(stripped[col_idx["qty"]])), clean_num(stripped[col_idx["price"]])
                    raw_rows.append((dt, sym_str, side_sign, qty, price))
                except: continue
    raw_rows.reverse()
    fills, occ_tracker = [], {}
    for seq_no, (dt, sym_str, side_sign, qty, price) in enumerate(raw_rows, 1):
        sig_base = (dt.strftime("%Y-%m-%d %H:%M:%S"), "", sym_str, side_sign, qty, round(price, 6))
        occ_tracker[sig_base] = occ_tracker.get(sig_base, 0) + 1
        fills.append({
            "dedup_key": sig_base + (occ_tracker[sig_base],),
            "datetime": dt, "seq": seq_no, "symbol": sym_str,
            "side_sign": side_sign, "qty": qty, "price": price, "fees": 0.0
        })
    return fills

def build_round_trip_trades(fills):
    df = pd.DataFrame(fills).drop_duplicates(subset=["dedup_key"])
    df = df.sort_values(["datetime", "seq"], kind="mergesort").reset_index(drop=True)
    hours = df["datetime"].dt.hour
    is_hkt_clock = ((hours >= 20) | (hours < 6)).mean() > 0.6
    open_books, closed_trades = {}, []

    for _, row in df.iterrows():
        sym = row["symbol"]
        qty, price, fee = float(row["qty"]), float(row["price"]), float(row["fees"])
        signed_qty = row["side_sign"] * qty
        dt = row["datetime"].to_pydatetime() if hasattr(row["datetime"], "to_pydatetime") else row["datetime"]

        if sym not in open_books or abs(open_books[sym]["pos_qty"]) < 1e-6:
            open_books[sym] = {
                "entry_time": dt, "last_entry_time": dt,
                "side": "Long" if signed_qty > 0 else "Short",
                "pos_qty": signed_qty, "entry_qty": qty, "entry_notional": qty * price,
                "exit_qty": 0.0, "exit_notional": 0.0,
                "avg_price": price, "realized_pnl": 0.0, "fees": fee, "execs": 1, "added_to_loser": False
            }
            continue

        book = open_books[sym]
        pos_qty = book["pos_qty"]
        if (pos_qty > 0 and signed_qty > 0) or (pos_qty < 0 and signed_qty < 0):
            sec_diff = (dt - book["last_entry_time"]).total_seconds()
            if sec_diff > 3 and ((pos_qty > 0 and price < book["avg_price"] - ADD_LOSER_BUFFER) or (pos_qty < 0 and price > book["avg_price"] + ADD_LOSER_BUFFER)):
                book["added_to_loser"] = True
            new_pos = abs(pos_qty) + qty
            book["avg_price"] = (abs(pos_qty) * book["avg_price"] + qty * price) / new_pos
            book["pos_qty"] += signed_qty
            book["entry_qty"] += qty
            book["entry_notional"] += qty * price
            book["fees"] += fee
            book["execs"] += 1
            book["last_entry_time"] = dt
        else:
            close_shares = min(abs(pos_qty), qty)
            if pos_qty > 0: book["realized_pnl"] += (price - book["avg_price"]) * close_shares
            else: book["realized_pnl"] += (book["avg_price"] - price) * close_shares
            book["pos_qty"] += signed_qty
            book["exit_qty"] += close_shares
            book["exit_notional"] += close_shares * price
            book["fees"] += fee
            book["execs"] += 1

            if abs(book["pos_qty"]) < 1e-6:
                net_pnl = round(book["realized_pnl"] - book["fees"], 2)
                shares = int(round(book["entry_qty"]))
                vol = int(round(book["entry_qty"] + book["exit_qty"]))
                open_px = round(book["entry_notional"] / book["entry_qty"], 2) if book["entry_qty"] else 0.0
                close_px = round(book["exit_notional"] / book["exit_qty"], 2) if book["exit_qty"] else price
                hold_sec = max(0, int(round((dt - book["entry_time"]).total_seconds())))
                session_date = (dt - datetime.timedelta(hours=12)).strftime("%Y-%m-%d") if USE_US_SESSION_DATE and is_hkt_clock else dt.strftime("%Y-%m-%d")
                et_open = hkt_to_us_eastern(book["entry_time"], is_hkt_clock)

                closed_trades.append({
                    "date": session_date, "month": session_date[:7], "year": session_date[:4],
                    "open_time": book["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                    "close_time": dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "close_hm": dt.strftime("%H:%M:%S"),
                    "us_session": classify_us_session(et_open),
                    "hold_sec": hold_sec, "exit_dt": dt,
                    "symbol": sym, "side": book["side"], "shares": shares, "volume": vol,
                    "open_price": open_px, "close_price": close_px, "comm": round(book["fees"], 2),
                    "execs": book["execs"], "net_pnl": net_pnl, "pnl_ps": round(net_pnl / shares, 4) if shares > 0 else 0.0,
                    "added_to_loser": book["added_to_loser"]
                })
    closed_trades.sort(key=lambda x: x["exit_dt"])
    for idx, t in enumerate(closed_trades, 1):
        t["no"] = idx
        del t["exit_dt"]
    return closed_trades

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-HK">
<head>
<meta charset="UTF-8">
<title>Open Source Trading Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root {
    --bg:#f8fafc; --card:#ffffff; --border:#e2e8f0; --text:#0f172a; --muted:#64748b;
    --green:#10b981; --green-bg:#ecfdf5; --red:#ef4444; --red-bg:#fef2f2;
    --blue:#2563eb; --blue-bg:#eff6ff; --purple:#7c3aed; --purple-bg:#f5f3ff;
    --forest:#27533b; --forest-light:#eef5f0;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-family:'Inter','Segoe UI',system-ui,sans-serif; display:flex; min-height:100vh; }
  .sidebar { width:215px; background:var(--card); border-right:1px solid var(--border); padding:20px 12px; position:fixed; top:0; bottom:0; left:0; display:flex; flex-direction:column; gap:4px; z-index:20; }
  .brand { font-size:15px; font-weight:800; color:var(--text); padding:6px 12px 18px; border-bottom:1px solid var(--border); margin-bottom:10px; display:flex; align-items:center; gap:8px; }
  .brand span { background:var(--blue); color:#fff; border-radius:6px; padding:3px 7px; font-size:12px; }
  .nav-btn { display:flex; align-items:center; gap:10px; width:100%; border:none; background:transparent; color:#475569; padding:10px 12px; border-radius:8px; font-size:13.5px; font-weight:600; cursor:pointer; text-align:left; transition:.15s; }
  .nav-btn:hover { background:#f1f5f9; color:var(--text); }
  .nav-btn.active { background:var(--blue-bg); color:var(--blue); }
  .sidebar-foot { margin-top:auto; font-size:11.5px; color:var(--muted); padding:12px; border-top:1px solid var(--border); line-height:1.5; }
  .main { margin-left:215px; flex:1; padding:22px 28px 50px; max-width:calc(100vw - 215px); }
  .topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap; margin-bottom:20px; background:var(--card); border:1px solid var(--border); border-radius:12px; padding:14px 18px; }
  .page-title { font-size:22px; font-weight:800; margin:0; }
  .acct-dates { font-size:12px; color:var(--muted); text-align:right; line-height:1.6; }
  .filters { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:8px; }
  select, input[type="date"], input[type="text"], input[type="number"] { border:1px solid var(--border); background:#fff; color:var(--text); border-radius:7px; padding:6px 10px; font-size:12.5px; outline:none; }
  .btn { border:1px solid var(--border); background:#fff; color:var(--text); border-radius:7px; padding:6px 12px; font-size:12.5px; font-weight:600; cursor:pointer; }
  .btn:hover { background:#f1f5f9; }
  .btn-primary { background:var(--blue); color:#fff; border-color:var(--blue); }
  .btn-primary:hover { background:#1d4ed8; }
  .btn-green { background:var(--green); color:#fff; border-color:var(--green); }
  .btn-green:hover { background:#059669; }
  .page { display:none; }
  .page.active { display:block; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; box-shadow:0 1px 2px rgba(15,23,42,.03); }
  
  .modal-overlay { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(15,23,42,0.75); z-index:100; backdrop-filter:blur(4px); }
  .modal-content { position:absolute; top:5%; left:50%; transform:translate(-50%, 0); width:85%; max-width:900px; background:var(--card); border-radius:12px; padding:24px; max-height:90vh; overflow-y:auto; box-shadow:0 10px 25px rgba(0,0,0,0.2); }
  .modal-header { display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding-bottom:12px; margin-bottom:16px; }
  .audit-questions { background:#f8fafc; border:1px solid var(--border); padding:16px; border-radius:8px; margin-bottom:16px; }
  .audit-row { display:flex; align-items:center; gap:12px; margin-bottom:8px; font-size:14px; font-weight:600; }
  .audit-row input[type="checkbox"] { width:18px; height:18px; cursor:pointer; }
  .paste-area { border:2px dashed #94a3b8; border-radius:8px; padding:24px; text-align:center; color:var(--muted); background:#f1f5f9; cursor:text; margin-bottom:16px; transition:0.2s; }
  .paste-area:focus { border-color:var(--blue); background:#eff6ff; outline:none; }
  .saved-images-container { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:16px; }
  .saved-images-container img { max-width:100%; border-radius:6px; border:1px solid var(--border); max-height:300px; object-fit:contain; }
  .img-wrapper { position:relative; display:inline-block; width:100%; }
  .del-img-btn { position:absolute; top:8px; right:8px; background:rgba(239,68,68,0.9); color:white; border:none; border-radius:4px; padding:4px 8px; cursor:pointer; font-size:12px; }
  textarea.note-input { width:100%; height:120px; border:1px solid var(--border); border-radius:8px; padding:12px; font-size:14px; font-family:inherit; resize:vertical; margin-bottom:16px; }
  
  .rendered-note { background:#fffbeb; border:1px solid #fde68a; border-radius:8px; padding:16px; margin-bottom:16px; }
  .rendered-q { margin:0 0 8px 0; font-size:13.5px; color:#475569; font-weight:600; }
  .pm-box { background:var(--forest-light); border:1px solid var(--forest); border-radius:8px; padding:14px; margin-bottom:16px; }
  .pm-title { font-size:14px; font-weight:800; color:var(--forest); margin-bottom:10px; display:flex; justify-content:space-between;}
  
  .grid-5 { display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:20px; }
  .ttp-card { border-radius:10px; padding:16px; position:relative; box-shadow:0 2px 4px rgba(0,0,0,0.2); display:flex; flex-direction:column; justify-content:space-between; min-height:120px; border:1px solid transparent; }
  .ttp-card.failed { border-color:#fca5a5 !important; background:#450a0a !important; }
  .ttp-head { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:12px; }
  .ttp-title { font-size:12.5px; font-weight:700; }
  .ttp-badge { font-size:10.5px; font-weight:800; padding:3px 7px; border-radius:5px; text-transform:uppercase; letter-spacing:0.5px; }
  .ttp-badge.pass { background:var(--green); color:#fff; }
  .ttp-badge.fail { background:var(--red); color:#fff; }
  .ttp-badge.warn { background:#475569; color:#f8fafc; }
  .ttp-val { font-size:22px; font-weight:800; margin-bottom:4px; letter-spacing:-0.5px; }
  .ttp-target { font-size:11.5px; font-weight:600; }
  .ttp-bar-bg { height:6px; border-radius:3px; overflow:hidden; margin-top:12px; position:relative; }
  .ttp-bar-fill { height:100%; border-radius:3px; transition: width 0.5s ease; }
  .ttp-bar-fill.pass { background:var(--green); }
  .ttp-bar-fill.fail { background:var(--red); }
  .ttp-bar-fill.warn { background:#64748b; }

  .grid-6 { display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:12px; }
  .grid-4 { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:14px; }
  .grid-3 { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-bottom:14px; }
  .grid-2 { display:grid; grid-template-columns:repeat(2,1fr); gap:14px; margin-bottom:14px; }
  .kpi-lbl { font-size:12px; color:var(--muted); font-weight:600; margin-bottom:6px; }
  .kpi-val { font-size:22px; font-weight:800; letter-spacing:-0.3px; }
  .kpi-sub { font-size:11.5px; color:#94a3b8; margin-top:5px; }
  .pos { color:var(--green); }
  .neg { color:var(--red); }
  .ratio-bar { height:10px; border-radius:6px; background:#fee2e2; overflow:hidden; display:flex; margin-top:8px; }
  .ratio-bar-win { background:var(--green); height:100%; }
  .ratio-bar-loss { background:var(--red); height:100%; }
  .chart-wrap { height:250px; position:relative; }
  .sec-title { font-size:14px; font-weight:700; margin:0 0 12px 0; display:flex; justify-content:space-between; align-items:center; }
  table.clean { width:100%; border-collapse:collapse; font-size:12.5px; }
  table.clean th { text-align:left; color:var(--muted); font-weight:600; padding:9px 8px; border-bottom:1px solid var(--border); background:#f8fafc; cursor:pointer; user-select:none; }
  table.clean th:hover { background:#f1f5f9; }
  table.clean td { padding:8.5px 8px; border-bottom:1px solid #f1f5f9; }
  table.clean tr:nth-child(even) td { background-color: #fafbfc; } 
  table.clean tr:last-child td { border-bottom:none; }
  .val-r { text-align:right; font-weight:700; font-variant-numeric:tabular-nums; }
  .subtabs { display:flex; gap:6px; border-bottom:1px solid var(--border); margin-bottom:16px; flex-wrap:wrap; }
  .subtab { border:1px solid transparent; border-bottom:none; background:transparent; color:var(--muted); padding:9px 15px; font-size:13px; font-weight:600; cursor:pointer; border-radius:8px 8px 0 0; }
  .subtab.active { background:var(--card); color:var(--blue); border-color:var(--border); margin-bottom:-1px; }
  .stat-card { text-align:center; padding:18px 12px; }
  .sym-pill { display:inline-block; background:var(--purple-bg); color:var(--purple); font-weight:700; font-size:12px; padding:3px 10px; border-radius:6px; margin:6px 0; }
  .cal-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; flex-wrap:wrap; gap:10px; }
  .cal-grid { display:grid; grid-template-columns:repeat(7,1fr); border-top:1px solid var(--border); border-left:1px solid var(--border); background:#fff; border-radius:10px; overflow:hidden; }
  .cal-dow { padding:10px; text-align:center; font-size:12px; font-weight:600; color:var(--muted); background:#f8fafc; border-right:1px solid var(--border); border-bottom:1px solid var(--border); }
  .cal-cell { min-height:108px; padding:8px; border-right:1px solid var(--border); border-bottom:1px solid var(--border); display:flex; flex-direction:column; justify-content:space-between; }
  .cal-cell.muted { background:#f8fafc; color:#cbd5e1; }
  .cal-box { border-radius:7px; padding:8px 10px; cursor:pointer; transition:.15s; }
  .cal-box.win { background:#eff6ff; border:1px solid #93c5fd; color:#1d4ed8; }
  .cal-box.loss { background:#fef2f2; border:1px solid #fca5a5; color:#dc2626; }
  .badge-closed { background:#f1f5f9; color:#334155; font-size:11px; font-weight:700; padding:3px 8px; border-radius:5px; border:1px solid #cbd5e1; }
  .pnl-pill { display:inline-block; color:#fff; font-weight:700; font-size:12px; padding:3px 9px; border-radius:5px; min-width:64px; text-align:center; }
  .pnl-pill.win { background:var(--green); }
  .pnl-pill.loss { background:#f87171; }
  .pnl-pill.flat { background:#94a3b8; }
  .tag-viol { background:#fef2f2; color:#dc2626; border:1px solid #fecaca; font-size:11px; font-weight:700; padding:2px 7px; border-radius:5px; }
  .tag-disc { background:#ecfdf5; color:#059669; font-size:11px; font-weight:600; padding:2px 7px; border-radius:5px; }
  .sim-layout { display:grid; grid-template-columns:330px 1fr; gap:18px; }
  .form-group { margin-bottom:13px; }
  .form-group label { display:block; font-size:12.5px; font-weight:700; margin-bottom:5px; text-align:center; }
  .form-group input { width:100%; padding:8px 10px; font-size:13.5px; text-align:center; }
  .day-card { margin-bottom:18px; }
  .day-top { display:grid; grid-template-columns:190px 170px 120px 1fr; gap:16px; align-items:center; padding:12px 0; border-bottom:1px solid var(--border); margin-bottom:12px; }
  .pagination { display:flex; justify-content:center; align-items:center; gap:8px; margin-top:16px; flex-wrap:wrap; }
  .compact-tbl { width:80%; margin:0 auto; font-size:13px; }
  @media (max-width:1280px) { .grid-6 { grid-template-columns:repeat(3,1fr); } .grid-4 { grid-template-columns:repeat(2,1fr); } .sim-layout { grid-template-columns:1fr; } .day-top { grid-template-columns:1fr 1fr; } .compact-tbl { width:100%; } }
</style>
</head>
<body>

<aside class="sidebar">
  <div class="brand"><span>Trade</span> Analytics Suite</div>
  <button class="nav-btn active" onclick="switchPage('dashboard', this)">⊞ Dashboard</button>
  <button class="nav-btn" onclick="switchPage('calendar', this)">📅 Calendar</button>
  <button class="nav-btn" onclick="switchPage('statistics', this)">📊 Statistics</button>
  <button class="nav-btn" onclick="switchPage('equity', this)">📈 Equity Curve</button>
  <button class="nav-btn" onclick="switchPage('trades', this)">📋 Trades</button>
  <button class="nav-btn" onclick="switchPage('notes', this)">📝 Daily Notes</button>
  <div class="sidebar-foot" id="sidebarFoot"></div>
</aside>

<main class="main">
  <div class="topbar">
    <div style="width:100%; display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:16px;">
      <div>
        <div style="display:flex; align-items:center; gap:12px; margin-bottom:8px; flex-wrap:wrap;">
          <h1 class="page-title" id="pageHeaderTitle">Dashboard</h1>
          <button class="btn btn-primary" style="padding:4px 10px; font-size:12px; border-radius:6px;" onclick="toggleDisplayMode()" id="btnDisplayMode">切換為 R 倍數</button>
          <!-- 全局 1R 設定 -->
          <div style="display:flex; align-items:center; background:#f1f5f9; padding:2px 8px; border-radius:6px; border:1px solid var(--border);">
            <label style="font-size:12px; font-weight:700; color:var(--muted); margin-right:4px;">固定風險 1R : $</label>
            <input type="number" id="inputOneR" value="25" step="5" style="width:60px; padding:2px; font-size:14px; font-weight:800; color:var(--blue); border:none; background:transparent; outline:none; text-align:center;" onchange="applyOneR()">
          </div>
        </div>
        <div class="filters">
          <select id="quickPeriod" onchange="applyQuickPeriod()">
            <option value="ALL">All Time (全部區間)</option>
            <option value="LATEST">Latest Day (最新交易日)</option>
          </select>
          <input type="date" id="startDate" onchange="applyFilters()">
          <span style="color:var(--muted);font-size:12px;">至</span>
          <input type="date" id="endDate" onchange="applyFilters()">
          <select id="sideFilter" onchange="applyFilters()">
            <option value="ALL">All Sides</option>
            <option value="Short">Short Only</option>
            <option value="Long">Long Only</option>
          </select>
          <select id="discFilter" onchange="applyFilters()">
            <option value="ALL">全部紀律狀態</option>
            <option value="OK">守紀律 (Disciplined)</option>
            <option value="VIOL">違規 (攤平 / 超額止損)</option>
          </select>
          <input type="text" id="symFilter" placeholder="輸入代號篩選" style="width:120px;" oninput="applyFilters()">
          <button class="btn" onclick="resetFilters()">重設</button>
        </div>
      </div>
      <!-- 帳戶起訖日移到右上角空白處 -->
      <div class="acct-dates" id="acctDateBox" style="text-align:right; background:#f8fafc; padding:8px 12px; border-radius:8px; border:1px solid var(--border);"></div>
    </div>
  </div>

  <!-- 1. DASHBOARD -->
  <section id="page-dashboard" class="page active">
    
    <!-- 90-Day Challenge Tracker (3-Stage Professional Ramp) -->
    <div style="background:#0f172a; border:1px solid #1e293b; border-radius:12px; padding:20px 24px; margin-bottom:24px; box-shadow:0 10px 15px -3px rgba(0,0,0,0.3); color:#f8fafc; position:relative;">
      
      <!-- 單行頂部：標題 + 中間核心數據 + 右側起算日控制器 -->
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; flex-wrap:wrap; gap:16px;">
        
        <!-- 左側：標題 -->
        <div style="font-size:18px; font-weight:800; display:flex; align-items:center; gap:8px;">
            <span>🚀 90-Day Challenge</span>
        </div>
        
        <!-- 中間：核心數據 (Active Days, EV Ratio, Stage) -->
        <div style="display:flex; gap:16px; align-items:center; background:#1e293b; padding:6px 16px; border-radius:8px; border:1px solid #334155;">
          <span style="font-size:12.5px; font-weight:700; color:#cbd5e1;" id="chalProgress">Active Days: 0 / 90</span>
          <span style="color:#475569;">|</span>
          <span style="font-size:12.5px; font-weight:700; color:#cbd5e1;" id="chalEV">EV Ratio: 0.00</span>
          <span style="color:#475569;">|</span>
          <span style="font-size:12px; font-weight:800; padding:3px 10px; border-radius:6px; background:#3b82f6; color:#fff;" id="chalStage">Stage 1 (1R=$25)</span>
        </div>

        <!-- 右側：起算日調整器 + 重置按鈕 + LIVE 標籤 -->
        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          <div style="display:flex; align-items:center; gap:6px; background:#1e293b; padding:4px 10px; border-radius:6px; border:1px solid #334155;">
            <label style="font-size:11.5px; font-weight:700; color:#94a3b8;">起算日:</label>
            <input type="date" id="chalStartDateInput" style="background:#0f172a; color:#f8fafc; border:1px solid #475569; border-radius:4px; padding:2px 6px; font-size:12px;" onchange="updateChallengeStartDate(this.value)">
          </div>
          <button class="btn" style="background:#ef4444; color:#fff; border:none; padding:5px 10px; font-size:11.5px; font-weight:800; border-radius:6px; cursor:pointer;" onclick="resetChallengeDateToday()">🔄 重置</button>
          <div style="background:var(--blue); color:#fff; font-size:10px; font-weight:800; padding:4px 8px; border-radius:4px; letter-spacing:1px;">LIVE</div>
        </div>
      </div>
      
      <!-- TTP-Style 3-Stage Progress Bar with Aligned Milestone Nodes & Floating Callout -->
      <div style="margin-bottom:24px; background:#1e293b; padding:18px 34px 20px 34px; border-radius:10px; border:1px solid #334155;">
          <!-- Top Row: Made vs Next Milestone Target + Floating TTP Callout Pill -->
          <div style="position:relative; height:28px; margin-bottom:12px;">
              <span id="pbMade" style="position:absolute; left:0; top:2px; transform:translateX(-14px); font-size:12.5px; font-weight:700; color:#cbd5e1; white-space:nowrap;">0.00R made</span>
              <span id="pbStage1TopLbl" style="position:absolute; left:21.43%; top:2px; transform:translateX(-50%); font-size:11.5px; font-weight:600; color:#64748b; white-space:nowrap;">15.00R target</span>
              <span id="pbStage2TopLbl" style="position:absolute; left:57.14%; top:2px; transform:translateX(-50%); font-size:11.5px; font-weight:600; color:#64748b; white-space:nowrap;">40.00R target</span>
              <span id="pbFinalTopLbl" style="position:absolute; right:0; top:2px; transform:translateX(14px); font-size:11.5px; font-weight:600; color:#94a3b8; white-space:nowrap;">70.00R target</span>
              <!-- Floating TTP Cyan Callout Pill with Pointer Arrow -->
              <div id="pbToGoWrap" style="position:absolute; left:0%; top:0px; transform:translateX(-50%); transition:left 0.8s cubic-bezier(0.4, 0, 0.2, 1); z-index:6; display:flex; flex-direction:column; align-items:center; pointer-events:none;">
                  <span id="pbToGo" style="background:#14b8a6; color:#042f2e; padding:2px 9px; border-radius:6px; font-size:11px; font-weight:800; white-space:nowrap; box-shadow:0 2px 6px rgba(0,0,0,0.35);">15.00R to Stage 2</span>
                  <div id="pbToGoArrow" style="width:0; height:0; border-left:5px solid transparent; border-right:5px solid transparent; border-top:5px solid #14b8a6; margin-top:-1px;"></div>
              </div>
          </div>

          <!-- Track & Perfectly Aligned Milestone Icons -->
          <div style="height:14px; background:#0f172a; border-radius:7px; position:relative; border:1px solid #334155; margin:10px 0 46px 0;">
              <!-- Pink Fill Bar -->
              <div id="pbFill" style="position:absolute; top:0; left:0; height:100%; width:0%; background:linear-gradient(90deg, #ec4899, #f472b6); border-radius:7px; transition:width 0.8s cubic-bezier(0.4, 0, 0.2, 1); z-index:1;"></div>

              <!-- Node 0: Start / Stage 1 (0% = 0R) -->
              <div id="nodeStage0" style="position:absolute; top:50%; left:0%; transform:translate(-50%, -50%); width:28px; height:28px; border-radius:50%; background:#ec4899; border:2px solid #f9a8d4; display:flex; align-items:center; justify-content:center; font-size:13px; z-index:3; box-shadow:0 0 8px rgba(236,72,153,0.5);" title="Start: Stage 1 (1R = $25)">🚀</div>
              <div style="position:absolute; top:22px; left:0%; transform:translateX(-12px); text-align:left; white-space:nowrap;">
                  <div style="font-size:11px; font-weight:600; color:#94a3b8;">Stage 1 (Start)</div>
                  <div style="font-size:13.5px; font-weight:800; color:#f8fafc;">1R = $25</div>
              </div>

              <!-- Node 1: Unlock Stage 2 (15R / 70R = 21.43%) -->
              <div id="nodeStage1" style="position:absolute; top:50%; left:21.43%; transform:translate(-50%, -50%); width:28px; height:28px; border-radius:50%; background:#1e293b; border:2px solid #475569; display:flex; align-items:center; justify-content:center; font-size:12px; z-index:3; transition:0.3s;" title="Unlock Stage 2: +15R & 10 Active Days">🔒</div>
              <div style="position:absolute; top:22px; left:21.43%; transform:translateX(-50%); text-align:center; white-space:nowrap;">
                  <div style="font-size:11px; font-weight:600; color:#94a3b8;">Stage 2 (+15R / 10d)</div>
                  <div style="font-size:13.5px; font-weight:800; color:#38bdf8;">1R = $50</div>
              </div>

              <!-- Node 2: Unlock Stage 3 (40R / 70R = 57.14%) -->
              <div id="nodeStage2" style="position:absolute; top:50%; left:57.14%; transform:translate(-50%, -50%); width:28px; height:28px; border-radius:50%; background:#1e293b; border:2px solid #475569; display:flex; align-items:center; justify-content:center; font-size:12px; z-index:3; transition:0.3s;" title="Unlock Stage 3: +40R & 25 Active Days">🔒</div>
              <div style="position:absolute; top:22px; left:57.14%; transform:translateX(-50%); text-align:center; white-space:nowrap;">
                  <div style="font-size:11px; font-weight:600; color:#94a3b8;">Stage 3 (+40R / 25d)</div>
                  <div style="font-size:13.5px; font-weight:800; color:#a78bfa;">1R = $100</div>
              </div>

              <!-- Node 3: Mastery Goal (70R / 70R = 100%) -->
              <div id="nodeStage3" style="position:absolute; top:50%; left:100%; transform:translate(-50%, -50%); width:28px; height:28px; border-radius:50%; background:#1e293b; border:2px solid #475569; display:flex; align-items:center; justify-content:center; font-size:13px; z-index:3; transition:0.3s;" title="90-Day Mastery Goal: +70R & 45 Active Days">🏆</div>
              <div style="position:absolute; top:22px; right:0%; transform:translateX(12px); text-align:right; white-space:nowrap;">
                  <div style="font-size:11px; font-weight:600; color:#94a3b8;">Mastery (+70R / 45d)</div>
                  <div style="font-size:13.5px; font-weight:800; color:#10b981;">Pro Funded</div>
              </div>
          </div>
      </div>

      <div class="grid-5" id="ttpObjectives" style="margin-bottom:0;"></div>
    </div>

    <div class="grid-6" id="dashRow1"></div>
    <div class="grid-4" id="dashRow2"></div>
    <div class="grid-3">
      <div class="card" style="grid-column: span 2;"><div class="sec-title"><span>Net P&L</span></div><div class="chart-wrap"><canvas id="cDashNet"></canvas></div></div>
      <div class="card" style="display:flex;flex-direction:column;justify-content:center;align-items:center;">
        <div class="sec-title" style="width:100%;"><span>🎮 交易能力值 (Trader Edge)</span></div>
        <div style="height:250px; width:100%; position:relative;"><canvas id="cRadar"></canvas></div>
      </div>
    </div>
    <div class="grid-2">
      <div class="card"><div class="sec-title"><span>Daily Win Rate</span></div><div class="chart-wrap"><canvas id="cDashWinRate"></canvas></div></div>
      <div class="card"><div class="sec-title"><span>Discipline Performance Gap</span><span id="gapBadge" style="font-size:12px;"></span></div><div class="chart-wrap"><canvas id="cDashGap"></canvas></div></div>
    </div>
    <div class="grid-3">
      <div class="card"><div class="sec-title">Trades info</div><div id="dashTradesInfo"></div></div>
      <div class="card"><div class="sec-title">Performance info</div><div id="dashPerfInfo"></div></div>
      <div class="card"><div class="sec-title">Symbols info</div><div id="dashSymInfo"></div></div>
    </div>
  </section>

<!-- 2. CALENDAR -->
  <section id="page-calendar" class="page">
    <div class="card">
      <div class="cal-head">
        <div style="font-size:18px;font-weight:800;" id="calTitleLabel">Calendar</div>
        <div style="display:flex;gap:8px;align-items:center;">
          <button class="btn" onclick="stepCalMonth(-1)">◀</button>
          <select id="calMonthSel" onchange="renderCalendar()"></select>
          <select id="calYearSel" onchange="renderCalendar()"></select>
          <button class="btn" onclick="stepCalMonth(1)">▶</button>
          <button class="btn btn-primary" id="btnCalMonth" onclick="setCalMode('month')">Month</button>
          <button class="btn" id="btnCalYear" onclick="setCalMode('year')">Year</button>
        </div>
      </div>
      <div id="calContainer"></div>
    </div>
  </section>

  <!-- 3. STATISTICS -->
  <section id="page-statistics" class="page">
    <div class="subtabs">
      <button class="subtab active" onclick="switchStatTab('pnl', this)">P&L Statistics</button>
      <button class="subtab" onclick="switchStatTab('trades', this)">Trades Statistics</button>
      <button class="subtab" onclick="switchStatTab('symbol', this)">Symbol Statistics</button>
      <button class="subtab" onclick="switchStatTab('volume', this)">Volume Statistics</button>
      <button class="subtab" onclick="switchStatTab('summary', this)">Summary Table</button>
      <button class="subtab" onclick="switchStatTab('session', this)">Time & Per-Share 分析</button>
    </div>
    <div id="statTabContent"></div>
  </section>

  <!-- 4. EQUITY CURVE -->
  <section id="page-equity" class="page">
    <div style="text-align:center;margin-bottom:18px;">
      <h2 style="margin:0 0 6px 0;font-size:21px;">Equity Curve Simulator</h2>
      <div style="color:var(--muted);font-size:13px;">根據你的真實統計數據模擬未來 12 個月（20 組隨機路徑）的潛在資金增長。</div>
    </div>
    <div class="sim-layout">
      <div class="card">
        <div class="sec-title" style="justify-content:center;">Simulation Parameters</div>
        <div class="form-group"><label>Starting Equity ($)</label><input type="number" id="simStart" step="500" oninput="runMonteCarlo()"></div>
        <div class="form-group"><label>Win Percentage (%)</label><input type="number" id="simWinPct" step="0.5" oninput="runMonteCarlo()"></div>
        <div class="form-group"><label>Average Winner / Loser Ratio</label><input type="number" id="simWlRatio" step="0.05" oninput="runMonteCarlo()"></div>
        <div class="form-group"><label>Average Trades per Month</label><input type="number" id="simTradesMo" step="1" oninput="runMonteCarlo()"></div>
        <div class="form-group"><label>Risk per Trade (Avg Loser $)</label><input type="number" id="simRisk" step="5" oninput="runMonteCarlo()"></div>
        <div style="display:flex;flex-direction:column;gap:8px;margin-top:14px;">
          <button class="btn btn-primary" onclick="runMonteCarlo()">🎲 重新隨機模擬 20 組路徑</button>
          <button class="btn btn-green" onclick="loadSimDefaults(true)">✨ 套用「機械鎖死 -1R」參數</button>
          <button class="btn" onclick="loadSimDefaults(false)">↺ Reset to Actual Stats</button>
        </div>
      </div>
      <div class="card">
        <div class="sec-title" style="justify-content:center;">Monte Carlo Equity Curve Simulation (20 Scenarios)</div>
        <div id="simSummaryBar" style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;background:#f8fafc;padding:12px;border-radius:8px;text-align:center;margin-bottom:14px;"></div>
        <div style="height:360px;position:relative;"><canvas id="cMonteCarlo"></canvas></div>
      </div>
    </div>
  </section>

  <!-- 5. TRADES -->
  <section id="page-trades" class="page">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:10px;">
        <div class="sec-title" style="margin:0;" id="tradesCountLabel">Trades</div>
        <div style="display:flex;gap:8px;align-items:center;">
          <span style="font-size:12px;color:var(--muted);">每頁顯示:</span>
          <select id="tradesPageSize" onchange="tradesPage=1;renderTradesPage()">
            <option value="10">10 / page</option>
            <option value="20" selected>20 / page</option>
            <option value="50">50 / page</option>
            <option value="100">100 / page</option>
          </select>
        </div>
      </div>
      <div style="overflow-x:auto;">
        <table class="clean" id="tradesFullTable"></table>
      </div>
      <div class="pagination" id="tradesPagination"></div>
    </div>
  </section>

  <!-- 6. DAILY NOTES -->
  <section id="page-notes" class="page">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:10px;">
      <div style="font-size:14px;color:var(--muted);" id="notesSubLabel">每日盤中走勢與黃金三問覆盤（支援截圖直接貼上）</div>
      <div style="display:flex;gap:8px;align-items:center;">
        <input type="date" id="notesJumpDate" onchange="jumpToNoteDate(this.value)">
        <button class="btn" onclick="clearNoteJump()">顯示全部日期</button>
        <button class="btn btn-primary" onclick="exportNotesJson()">💾 匯出筆記備份</button>
      </div>
    </div>
    <div id="dailyNotesContainer"></div>
    <div class="pagination" id="notesPagination"></div>
  </section>
</main>


<!-- 覆盤編輯彈出視窗 (Modal) -->
<div class="modal-overlay" id="auditModal">
  <div class="modal-content">
    <div class="modal-header">
      <h2 style="margin:0; font-size:20px;" id="modalTitle">交易覆盤筆記</h2>
      <button class="btn" onclick="closeAuditModal()">✕ 關閉</button>
    </div>
    
    <div style="display:grid; grid-template-columns: 1.5fr 1fr; gap:20px; margin-bottom:16px;">
        <div class="audit-questions" style="margin:0;">
          <div style="font-size:13px; color:var(--muted); margin-bottom:12px;">過程導向三問法 (Process Goals)</div>
          <div class="audit-row"><input type="checkbox" id="q1"> <label for="q1">1. 更有選擇性 (Be Selective)：進場是否完全符合你的交易策略與型態條件？</label></div>
          <div class="audit-row"><input type="checkbox" id="q2"> <label for="q2">2. 執行力 (Execute)：是否在進場同時預掛停損，且中途未手動擴大虧損？</label></div>
          <div class="audit-row"><input type="checkbox" id="q3"> <label for="q3">3. 持倉更久 (Hold Longer)：獲利單是否有按計畫抱到目標價，而非因為恐懼提早平倉？</label></div>
        </div>
        
        <div class="audit-questions" style="margin:0; background:#eff6ff; border-color:#bfdbfe;">
          <div style="font-size:14px; font-weight:800; color:#1d4ed8; margin-bottom:12px;">🏆 今日綜合評級 (Daily Grade)</div>
          <select id="modalGrade" style="width:100%; font-size:16px; font-weight:800; padding:10px; border-color:#93c5fd; color:#1e40af; border-radius:8px;">
            <option value="A">A 級 (完美執行過程目標，無論輸贏)</option>
            <option value="B">B 級 (有微小失誤，但迅速修正未釀大錯)</option>
            <option value="C">C 級 (情緒化、違背策略或扛單)</option>
            <option value="-">未評級</option>
          </select>
          <div style="font-size:11px; color:#64748b; margin-top:8px;">* The most effective daily goals emphasize trading well, not making money. (Oliver)</div>
        </div>
    </div>

    <textarea class="note-input" id="modalNoteText" placeholder="在此輸入其他細節檢討..."></textarea>
    <div style="font-size:14px; font-weight:600; margin-bottom:8px;">圖表截圖紀錄 (Chart Screenshots)</div>
    <div class="paste-area" id="pasteArea" tabindex="0">
      點擊此處並按下 <b>Ctrl + V</b> (或 Cmd + V) 貼上 TradingView / TOS 截圖<br>
      <span style="font-size:12px; color:var(--muted);">圖片將直接儲存於瀏覽器底層資料庫中，不影響電腦容量</span>
    </div>
    <div class="saved-images-container" id="modalImageContainer"></div>
    <div style="display:flex; justify-content:flex-end; gap:12px; margin-top:20px;">
      <button class="btn" onclick="closeAuditModal()">取消</button>
      <button class="btn btn-primary" onclick="saveAuditData()">💾 儲存紀錄</button>
    </div>
  </div>
</div>
<script>
const PAYLOAD = __PAYLOAD_JSON__;
const ALL_TRADES = PAYLOAD.trades;
const PM_DB = PAYLOAD.premarket_db || {};
const SLIPPAGE_TOLERANCE = 1.15;
let filtered = [];

let ONE_R = parseFloat(localStorage.getItem('app_user_1r')) || 25.0;
let challengeStartDate = localStorage.getItem('app_challenge_start') || PAYLOAD.chal_start || '2024-01-01';

function resetChallengeDateToday() {
    if (confirm("確定要將 90 日挑戰起算日設為今天嗎？")) {
        const todayStr = new Date().toISOString().slice(0, 10);
        challengeStartDate = todayStr;
        localStorage.setItem('app_challenge_start', challengeStartDate);
        const inputEl = document.getElementById('chalStartDateInput');
        if(inputEl) inputEl.value = challengeStartDate;
        refreshActiveView();
        alert(`挑戰起算日已更新為：${challengeStartDate}`);
    }
}

function updateChallengeStartDate(val) {
    if (val) {
        challengeStartDate = val;
        localStorage.setItem('app_challenge_start', challengeStartDate);
        refreshActiveView();
    }
}

let activePage = 'dashboard';
let activeStatTab = 'summary';
let calMode = 'month';
let calYear = 2026, calMonth = 10;
let tradesPage = 1, notesPage = 1, singleNoteDate = '';
let chartMap = {};

let sortCol = null;
let sortAsc = true;


let displayMode = localStorage.getItem('app_display_mode') || 'usd';
function toggleDisplayMode() {
    displayMode = displayMode === 'usd' ? 'r' : 'usd';
    localStorage.setItem('app_display_mode', displayMode);
    
    const btn = document.getElementById('btnDisplayMode');
    if (btn) {
        btn.innerText = displayMode === 'usd' ? '切換為 R 倍數' : '切換為 $ 金額';
        btn.className = displayMode === 'usd' ? 'btn btn-primary' : 'btn btn-green';
    }
    refreshActiveView();
}

const fmtMoney = (v, forceSign=false) => {
  const n = Number(v || 0);
  if (displayMode === 'r') {
      const rVal = n / ONE_R;
      const rStr = Math.abs(rVal).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) + 'R';
      if (n < 0) return `-${rStr}`;
      return (forceSign && n > 0 ? `+${rStr}` : `${rStr}`);
  } else {
      const abs = Math.abs(n).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
      if (n < 0) return `-$${abs}`;
      return (forceSign && n > 0 ? `+$${abs}` : `$${abs}`);
  }
};
const fmtInt = v => Math.round(Number(v || 0)).toLocaleString('en-US');
const fmtHold = sec => {
  if (!sec || sec < 10) return 'a few seconds';
  if (sec < 60) return `${Math.round(sec)} seconds`;
  if (sec < 3600) return `${Math.round(sec / 60)} minutes`;
  const h = sec / 3600;
  return h < 1.5 ? 'an hour' : `${h.toFixed(1)} hours`;
};
const fmtHoldShort = sec => {
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ${sec%60}s`;
  return `${Math.floor(sec/3600)}h ${Math.floor((sec%3600)/60)}m`;
};
function destroyChart(id) { if (chartMap[id]) { chartMap[id].destroy(); delete chartMap[id]; } }
function makeChart(id, cfg) {
  destroyChart(id);
  const el = document.getElementById(id);
  if (el) chartMap[id] = new Chart(el, cfg);
}

// 核心功能：切換 1R 並即時重算所有交易數據
function applyOneR() {
    const val = parseFloat(document.getElementById('inputOneR').value);
    if (!isNaN(val) && val > 0) {
        ONE_R = val;
        localStorage.setItem('app_user_1r', ONE_R);
    }
    document.getElementById('inputOneR').value = ONE_R;
    const max_loss = -(ONE_R * SLIPPAGE_TOLERANCE);
    
    ALL_TRADES.forEach(t => {
        t.exceeded_1r = t.net_pnl < max_loss;
        t.shadow_pnl = t.exceeded_1r ? -ONE_R : t.net_pnl;
        t.r_mult = t.net_pnl / ONE_R;
        t.disciplined = (!t.added_to_loser) && (!t.exceeded_1r);
        
        let tags = [];
        if (t.added_to_loser) tags.push("#AddedToLoser");
        if (t.exceeded_1r) tags.push("#MovedStop_Over1R");
        t.tags = tags.length ? tags.join(" ") : "Disciplined";
    });
    
    document.getElementById('sidebarFoot').innerHTML = `固定風控 1R = <b>$${ONE_R.toFixed(2)}</b><br>總平倉筆數: <b>${ALL_TRADES.length}</b>`;
    applyFilters();
}

let db;
const request = indexedDB.open("TradingAuditDB", 1);
request.onupgradeneeded = function(e) {
    db = e.target.result;
    if (!db.objectStoreNames.contains("images")) db.createObjectStore("images", { keyPath: "id" });
};
request.onsuccess = function(e) { db = e.target.result; applyOneR(); };

function saveImageToDB(dateStr, base64Data) {
    const id = dateStr + "_" + Date.now();
    const transaction = db.transaction(["images"], "readwrite");
    transaction.objectStore("images").add({ id: id, date: dateStr, data: base64Data });
    return id;
}
function getImagesForDate(dateStr, callback) {
    if (!db) return callback([]);
    const transaction = db.transaction(["images"], "readonly");
    const req = transaction.objectStore("images").getAll();
    req.onsuccess = function() { callback(req.result.filter(img => img.date === dateStr)); };
}
function deleteImageFromDB(id, callback) {
    const transaction = db.transaction(["images"], "readwrite");
    const req = transaction.objectStore("images").delete(id);
    req.onsuccess = function() { if(callback) callback(); };
}

let currentAuditDate = '';
function editDayNote(dateStr) {
    currentAuditDate = dateStr;
    document.getElementById('modalTitle').innerText = `${dateStr} 交易覆盤筆記`;
    document.getElementById('auditModal').style.display = 'block';
    
    const notes = JSON.parse(localStorage.getItem('trading_daily_notes_open') || '{}');
    const data = notes[dateStr] || { text: '', q1: false, q2: false, q3: false, grade: '-' };
    
    document.getElementById('modalNoteText').value = data.text;
    document.getElementById('q1').checked = data.q1;
    document.getElementById('q2').checked = data.q2;
    document.getElementById('q3').checked = data.q3;
    document.getElementById('modalGrade').value = data.grade || '-';
    
    const imgContainer = document.getElementById('modalImageContainer');
    imgContainer.innerHTML = '';
    getImagesForDate(dateStr, function(images) {
        images.forEach(img => {
            const div = document.createElement('div');
            div.className = 'img-wrapper'; div.id = 'img_' + img.id;
            div.innerHTML = `<img src="${img.data}"><button class="del-img-btn" onclick="removeImage('${img.id}')">刪除</button>`;
            imgContainer.appendChild(div);
        });
    });
}
function closeAuditModal() { document.getElementById('auditModal').style.display = 'none'; }
function saveAuditData() {
    const data = {
        text: document.getElementById('modalNoteText').value.trim(),
        q1: document.getElementById('q1').checked,
        q2: document.getElementById('q2').checked,
        q3: document.getElementById('q3').checked,
        grade: document.getElementById('modalGrade').value
    };
    const notes = JSON.parse(localStorage.getItem('trading_daily_notes_open') || '{}');
    notes[currentAuditDate] = data;
    localStorage.setItem('trading_daily_notes_open', JSON.stringify(notes));
    closeAuditModal(); renderDailyNotes();
}
function removeImage(id) {
    if (confirm("確定要刪除這張截圖嗎？")) {
        deleteImageFromDB(id, function() { document.getElementById('img_' + id).remove(); });
    }
}
document.getElementById('pasteArea').addEventListener('paste', function(e) {
    e.preventDefault();
    let items = (e.clipboardData || e.originalEvent.clipboardData).items;
    for (let index in items) {
        let item = items[index];
        if (item.kind === 'file' && item.type.indexOf('image/') !== -1) {
            let blob = item.getAsFile();
            let reader = new FileReader();
            reader.onload = function(event) {
                let base64 = event.target.result;
                let newId = saveImageToDB(currentAuditDate, base64);
                const div = document.createElement('div');
                div.className = 'img-wrapper'; div.id = 'img_' + newId;
                div.innerHTML = `<img src="${base64}"><button class="del-img-btn" onclick="removeImage('${newId}')">刪除</button>`;
                document.getElementById('modalImageContainer').appendChild(div);
            };
            reader.readAsDataURL(blob);
        }
    }
});
function exportNotesJson() {
    const blob = new Blob([localStorage.getItem('trading_daily_notes_open') || '{}'], {type:'application/json'});
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'trading_daily_notes_backup.json'; a.click();
}
function openNoteForDate(dStr) {
  singleNoteDate = dStr;
  document.getElementById('notesJumpDate').value = dStr;
  switchPage('notes', document.querySelectorAll('.nav-btn')[5]);
}
function jumpToNoteDate(val) { singleNoteDate = val; notesPage = 1; renderDailyNotes(); }
function clearNoteJump() { singleNoteDate = ''; document.getElementById('notesJumpDate').value = ''; notesPage = 1; renderDailyNotes(); }

function computeCoreStats(list) {
  const n = list.length;
  const empty = { n:0, wins:[], losses:[], bes:[], net:0, shadowNet:0, leak:0, winPct:0, avgWin:0, avgLoss:0, wlDollar:0, wlCount:0, maxWin:null, maxLoss:null, days:[], dayWinPct:0, avgTradesDay:0, discPct:0, discCnt:0, totalExecs:0, totalComm:0, totalVol:0, winVol:0, lossVol:0, maxWStreak:0, maxLStreak:0, months:[], years:[], symbols:[] };
  if (!n) return empty;

  const wins = list.filter(t => t.net_pnl > 0);
  const losses = list.filter(t => t.net_pnl < 0);
  const bes = list.filter(t => t.net_pnl === 0);
  const net = list.reduce((s,t) => s + t.net_pnl, 0);
  const shadowNet = list.reduce((s,t) => s + t.shadow_pnl, 0);
  const grossWin = wins.reduce((s,t) => s + t.net_pnl, 0);
  const grossLoss = losses.reduce((s,t) => s + t.net_pnl, 0);
  const avgWin = wins.length ? grossWin / wins.length : 0;
  const avgLoss = losses.length ? grossLoss / losses.length : 0;
  const wlDollar = avgLoss !== 0 ? Math.abs(avgWin / avgLoss) : 0;
  const wlCount = losses.length ? wins.length / losses.length : wins.length;

  let maxWin = list[0], maxLoss = list[0];
  let curW = 0, curL = 0, maxWStreak = 0, maxLStreak = 0;
  const dayMap = {}, moMap = {}, yrMap = {}, symMap = {};

  list.forEach(t => {
    if (t.net_pnl > maxWin.net_pnl) maxWin = t;
    if (t.net_pnl < maxLoss.net_pnl) maxLoss = t;
    if (t.net_pnl > 0) { curW++; curL = 0; if (curW > maxWStreak) maxWStreak = curW; }
    else if (t.net_pnl < 0) { curL++; curW = 0; if (curL > maxLStreak) maxLStreak = curL; }
    else { curW = 0; curL = 0; }

    if (!dayMap[t.date]) dayMap[t.date] = { date:t.date, net:0, shadow:0, trades:[], wins:0, losses:0, vol:0, winVol:0, lossVol:0, comm:0 };
    const d = dayMap[t.date];
    d.net += t.net_pnl; d.shadow += t.shadow_pnl; d.trades.push(t); d.vol += t.volume; d.comm += t.comm;
    if (t.net_pnl > 0) { d.wins++; d.winVol += t.volume; } else if (t.net_pnl < 0) { d.losses++; d.lossVol += t.volume; }

    moMap[t.month] = (moMap[t.month] || 0) + t.net_pnl;
    yrMap[t.year] = (yrMap[t.year] || 0) + t.net_pnl;

    if (!symMap[t.symbol]) symMap[t.symbol] = { symbol:t.symbol, cnt:0, wins:0, losses:0, net:0, vol:0, viols:0 };
    const sm = symMap[t.symbol];
    sm.cnt++; sm.net += t.net_pnl; sm.vol += t.volume;
    if (t.net_pnl > 0) sm.wins++; else if (t.net_pnl < 0) sm.losses++;
    if (!t.disciplined) sm.viols++;
  });

  const days = Object.values(dayMap).sort((a,b) => a.date.localeCompare(b.date));
  const winDays = days.filter(d => d.net > 0).length;
  const discCnt = list.filter(t => t.disciplined).length;

  return {
    n, wins, losses, bes, net, shadowNet, leak: shadowNet - net,
    winPct: (wins.length / n) * 100, avgWin, avgLoss, wlDollar, wlCount,
    maxWin, maxLoss, days, winDays, dayWinPct: days.length ? (winDays / days.length) * 100 : 0,
    avgTradesDay: days.length ? n / days.length : 0,
    discCnt, discPct: (discCnt / n) * 100,
    totalExecs: list.reduce((s,t) => s + t.execs, 0),
    totalComm: list.reduce((s,t) => s + t.comm, 0),
    totalVol: list.reduce((s,t) => s + t.volume, 0),
    winVol: wins.reduce((s,t) => s + t.volume, 0),
    lossVol: losses.reduce((s,t) => s + t.volume, 0),
    maxWStreak, maxLStreak,
    months: Object.entries(moMap).map(([m,v]) => ({m, net:v})),
    years: Object.entries(yrMap).map(([y,v]) => ({y, net:v})),
    symbols: Object.values(symMap)
  };
}

function renderDashboard(s) {
  if (!s.n) { document.getElementById('dashRow1').innerHTML = '<div class="card">無符合條件的交易紀錄</div>'; return; }
  const firstD = s.days[0].date, lastD = s.days[s.days.length - 1].date;
  document.getElementById('dashRow1').innerHTML = `
    <div class="card"><div class="kpi-lbl">Net P&L</div><div class="kpi-val ${s.net>=0?'pos':'neg'}">${fmtMoney(s.net)}</div><div class="kpi-sub">${firstD} ~ ${lastD}</div></div>
    <div class="card"><div class="kpi-lbl">Win %</div><div class="kpi-val">${s.winPct.toFixed(1)}%</div><div class="kpi-sub">${s.wins.length} wins / ${s.losses.length} losses</div></div>
    <div class="card"><div class="kpi-lbl">Largest Win</div><div class="kpi-val pos">${fmtMoney(s.maxWin.net_pnl)}</div><div class="kpi-sub">${s.maxWin.symbol} (${s.maxWin.date})</div></div>
    <div class="card"><div class="kpi-lbl">Day Win (%)</div><div class="kpi-val">${s.dayWinPct.toFixed(1)}%</div><div class="kpi-sub">${s.winDays} winning days</div></div>
    <div class="card"><div class="kpi-lbl">Avg Trades/day</div><div class="kpi-val">${s.avgTradesDay.toFixed(2)}</div><div class="kpi-sub">${s.days.length} active trading days</div></div>
    <div class="card"><div class="kpi-lbl">Discipline %</div><div class="kpi-val ${s.discPct>=85?'pos':'neg'}">${s.discPct.toFixed(1)}%</div><div class="kpi-sub">${s.discCnt} / ${s.n} 筆零攤平未超額</div></div>
  `;
  const wShare = (s.avgWin + Math.abs(s.avgLoss)) > 0 ? (s.avgWin / (s.avgWin + Math.abs(s.avgLoss)) * 100) : 100;
  document.getElementById('dashRow2').innerHTML = `
    <div class="card"><div class="kpi-lbl">Average Winner</div><div class="kpi-val">${fmtMoney(s.avgWin)}</div></div>
    <div class="card"><div class="kpi-lbl">Average Winner/Loser</div><div class="kpi-val">${s.wlDollar.toFixed(2)}</div>
      <div class="ratio-bar"><div class="ratio-bar-win" style="width:${wShare}%"></div><div class="ratio-bar-loss" style="width:${100-wShare}%"></div></div></div>
    <div class="card"><div class="kpi-lbl">Average Loser</div><div class="kpi-val">${fmtMoney(Math.abs(s.avgLoss))}</div></div>
    <div class="card"><div class="kpi-lbl">Largest Loss</div><div class="kpi-val neg">${fmtMoney(s.maxLoss.net_pnl)}</div><div class="kpi-sub">${s.maxLoss.symbol} (${s.maxLoss.date})</div></div>
  `;

  let cum = 0, cumS = 0;
  const dLabels = s.days.map(d => d.date.slice(5));
  const eqArr = s.days.map(d => { cum += d.net; return Number(cum.toFixed(2)); });
  const shArr = s.days.map(d => { cumS += d.shadow; return Number(cumS.toFixed(2)); });

  
  // ---------------- 90-Day Challenge & 3-Stage Unlock Tracker ----------------
  const CHAL_START = challengeStartDate;
  const dateInputEl = document.getElementById('chalStartDateInput');
  if (dateInputEl && dateInputEl.value !== CHAL_START) {
      dateInputEl.value = CHAL_START;
  }

  const chalTrades = ALL_TRADES.filter(t => t.date >= CHAL_START);
  const cStats = computeCoreStats(chalTrades);
  
  const trDays = cStats.days.length; 
  const pgEl = document.getElementById('chalProgress');
  if(pgEl) pgEl.innerText = `Active Days: ${trDays} / 90`;
  
  const winR = cStats.winPct / 100.0;
  const evRatio = cStats.n > 0 ? (winR * cStats.wlDollar) - (1 - winR) : 0;
  const totalR = cStats.net / ONE_R;

  let peakR = 0, maxDdR = 0, curR = 0;
  chalTrades.forEach(t => {
      curR += (t.net_pnl / ONE_R);
      if (curR > peakR) peakR = curR;
      const dd = curR - peakR;
      if (dd < maxDdR) maxDdR = dd;
  });
  const passDD = maxDdR >= -12.0;
  const pctDD = Math.min(100, Math.max(0, (1 - (Math.abs(maxDdR) / 12.0)) * 100));
  
  const bestTradeR = cStats.maxWin ? (cStats.maxWin.net_pnl / ONE_R) : 0;
  const bestTradePct = totalR > 0 ? (bestTradeR / totalR) * 100 : 0;
  const passConsist = (totalR > 0 && bestTradePct <= 40.0) || totalR <= 0;
  const pctConsist = totalR > 0 ? Math.max(0, 100 - (bestTradePct - 40)) : 100;
  
  // Count Violation Days (any trading day with >= 1 rule-violating trade: #MovedStop_Over1R or #AddedToLoser)
  // 3 Strikes Rule: 0, 1, 2 violation days = Passing (buffer); reaching 3/3 = Breached!
  const violDays = cStats.days.filter(d => d.trades.some(t => !t.disciplined)).length;
  const passDisc = violDays < 3;
  const pctDisc = passDisc ? Math.max(0, ((3 - violDays) / 3.0) * 100) : 100;
  const discSubTxt = !passDisc
      ? "Quota used up (3/3) — Reset required"
      : (violDays === 2 ? "⚠️ Last strike! Next day = Breached" : `${3 - violDays} strikes left (3rd day = Breached)`);

  // Professional 3-Stage Scaling Roadmap:
  // Stage 1 (1R=$25) -> Unlock Stage 2 at +15R & 10 Active Days
  // Stage 2 (1R=$50) -> Unlock Stage 3 at +40R & 25 Active Days
  // Stage 3 (1R=$100) -> Complete Mastery at +70R & 45 Active Days
  const finalTargetR = 70.0;
  const pctTrack = Math.min(100, Math.max(0, (totalR / finalTargetR) * 100));

  const unlockedS2 = (totalR >= 15.0 && trDays >= 10 && passDD && passDisc);
  const unlockedS3 = (totalR >= 40.0 && trDays >= 25 && passDD && passDisc);
  const unlockedMastery = (totalR >= 70.0 && trDays >= 45 && passDD && passDisc && passConsist);

  let currentStageName = "Stage 1 (1R=$25)";
  let nextTargetR = 15.0;
  let nextMinDays = 10;
  let nextStageLabel = "Stage 2";

  if (unlockedMastery) {
      currentStageName = "Mastery Pro 🏆 (1R=$100+)";
      nextTargetR = 70.0;
      nextMinDays = 45;
      nextStageLabel = "Mastery";
  } else if (unlockedS3 || totalR >= 40.0) {
      currentStageName = unlockedS3 ? "Stage 3 UNLOCKED 🔓 (1R=$100)" : "Stage 2 (Pending Days/Rules)";
      nextTargetR = 70.0;
      nextMinDays = 45;
      nextStageLabel = "Mastery";
  } else if (unlockedS2 || totalR >= 15.0) {
      currentStageName = unlockedS2 ? "Stage 2 UNLOCKED 🔓 (1R=$50)" : "Stage 1 (Pending Days/Rules)";
      nextTargetR = 40.0;
      nextMinDays = 25;
      nextStageLabel = "Stage 3";
  }

  // Update Progress Fill & Floating TTP Callout Pill
  const pbMadeEl = document.getElementById('pbMade');
  const pbToGoEl = document.getElementById('pbToGo');
  const pbToGoWrap = document.getElementById('pbToGoWrap');
  const pbToGoArrow = document.getElementById('pbToGoArrow');
  const pbFillEl = document.getElementById('pbFill');

  if (pbMadeEl) {
      pbMadeEl.innerText = `${totalR > 0 ? '+' : ''}${totalR.toFixed(2)}R made`;
      // Hide static left label if floating pill is right on top of 0% to prevent overlap
      pbMadeEl.style.opacity = pctTrack < 12 ? '0.35' : '1';
  }
  if (pbFillEl) pbFillEl.style.width = `${pctTrack}%`;

  const rRemaining = Math.max(0, nextTargetR - totalR);
  if (pbToGoEl && pbToGoWrap && pbToGoArrow) {
      let pillBg = '#14b8a6';
      let pillFg = '#042f2e';
      if (totalR >= finalTargetR) {
          pbToGoEl.innerText = `0.00R to go · Completed! 🏆`;
          pillBg = '#10b981'; pillFg = '#ffffff';
      } else if (rRemaining <= 0) {
          pbToGoEl.innerText = `0.00R to go (${Math.max(0, nextMinDays - trDays)}d left)`;
          pillBg = '#38bdf8'; pillFg = '#0f172a';
      } else {
          pbToGoEl.innerText = `${rRemaining.toFixed(2)}R to ${nextStageLabel}`;
      }
      pbToGoEl.style.background = pillBg;
      pbToGoEl.style.color = pillFg;
      pbToGoArrow.style.borderTopColor = pillBg;
      // Move pill dynamically along the bar (clamped 5%..94% so pill never overflows edges)
      const clampedLeft = Math.min(94, Math.max(5, pctTrack));
      pbToGoWrap.style.left = `${clampedLeft}%`;
  }

  // Update Milestone Node Icons along the bar
  const styleNode = (id, reached, unlocked, activeColor) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (unlocked) {
          el.innerText = id === 'nodeStage3' ? '🏆' : '🔓';
          el.style.background = activeColor;
          el.style.borderColor = '#ffffff';
          el.style.boxShadow = `0 0 10px ${activeColor}`;
      } else if (reached) {
          el.innerText = '⏳';
          el.style.background = '#0f172a';
          el.style.borderColor = activeColor;
          el.style.boxShadow = 'none';
      } else {
          el.innerText = id === 'nodeStage3' ? '🏆' : '🔒';
          el.style.background = '#1e293b';
          el.style.borderColor = '#475569';
          el.style.boxShadow = 'none';
      }
  };
  styleNode('nodeStage1', totalR >= 15.0, unlockedS2, '#38bdf8');
  styleNode('nodeStage2', totalR >= 40.0, unlockedS3, '#a78bfa');
  styleNode('nodeStage3', totalR >= 70.0, unlockedMastery, '#10b981');

  const stgEl = document.getElementById('chalStage');
  if (stgEl) {
      stgEl.innerText = currentStageName;
      stgEl.style.backgroundColor = unlockedMastery ? "#10b981" : (unlockedS3 ? "#7c3aed" : (unlockedS2 ? "#0d9488" : "#3b82f6"));
  }
  
  const evEl = document.getElementById('chalEV');
  if (evEl) evEl.innerHTML = `EV Ratio: <span style="color:${evRatio>=0?'#34d399':'#f87171'};">${evRatio>0?'+':''}${evRatio.toFixed(2)}R</span>`;
  
  const passStageProfit = totalR >= nextTargetR;
  const pctStageProfit = Math.min(100, Math.max(0, (totalR / nextTargetR) * 100));
  const passSample = trDays >= nextMinDays;
  const pctSample = Math.min(100, (trDays / nextMinDays) * 100);

  const ttpCard = (title, val, target, pass, pct, failIsRed=false) => {
      const badgeCls = pass ? 'pass' : (failIsRed ? 'fail' : 'warn');
      const badgeTxt = pass ? 'Passing' : (failIsRed ? 'Breached' : 'In progress');
      const barCls = pass ? 'pass' : (failIsRed ? 'fail' : 'warn');
      return `
      <div class="ttp-card ${failIsRed && !pass ? 'failed' : ''}" style="background:#0f172a; border-color:${failIsRed && !pass ? '#ef4444' : '#334155'};">
        <div>
          <div class="ttp-head">
            <span class="ttp-title" style="color:#94a3b8;">${title}</span>
            <span class="ttp-badge ${badgeCls}">${badgeTxt}</span>
          </div>
          <div class="ttp-val" style="color:#f8fafc;">${val}</div>
          <div class="ttp-target" style="color:#64748b;">${target}</div>
        </div>
        <div class="ttp-bar-bg" style="background:#1e293b;"><div class="ttp-bar-fill ${barCls}" style="width:${pct}%;"></div></div>
      </div>`;
  };

  const objEl = document.getElementById('ttpObjectives');
  if (objEl) {
      objEl.innerHTML = `
        ${ttpCard(`Profit Goal (${nextStageLabel})`, `${totalR>0?'+':''}${totalR.toFixed(2)}R`, `Next Unlock: +${nextTargetR.toFixed(0)}R (Max 70R)`, passStageProfit, pctStageProfit)}
        ${ttpCard("Max Drawdown", `${maxDdR.toFixed(2)}R`, "Limit: > -12.00R", passDD, pctDD, true)}
        ${ttpCard("Consistency Rule", totalR>0 ? `${bestTradePct.toFixed(1)}%` : 'N/A', "Max 40% best trade", passConsist, pctConsist, true)}
        ${ttpCard(`Active Days (${nextStageLabel})`, `${trDays} / ${nextMinDays}`, `Min ${nextMinDays} days for ${nextStageLabel}`, passSample, pctSample)}
        ${ttpCard("Discipline (3 Strikes)", `${violDays} / 3 Days`, discSubTxt, passDisc, pctDisc, true)}
      `;
  }

  // 計算雷達圖能力值 (滿分100)
  const scoreWinRate = Math.min(100, Math.max(0, (s.winPct / 65) * 100));
  const scorePF = Math.min(100, Math.max(0, (s.wlDollar / 1.5) * 100));
  const scoreConsist = Math.min(100, Math.max(0, (s.dayWinPct / 80) * 100));
  const scoreDisc = s.discPct; 
  let scoreFreq = 100;
  if (s.avgTradesDay <= 1) scoreFreq = 70;
  else if (s.avgTradesDay <= 4) scoreFreq = 100;
  else if (s.avgTradesDay <= 6) scoreFreq = 80;
  else scoreFreq = Math.max(0, 100 - (s.avgTradesDay - 6) * 15);
  let scoreRisk = 100;
  if (s.avgWin > 0 && Math.abs(s.maxLoss.net_pnl) > 0) {
      const riskRatio = Math.abs(s.maxLoss.net_pnl) / s.avgWin;
      scoreRisk = Math.max(0, Math.min(100, 100 - ((riskRatio - 1.5) / 2.5) * 100));
  }

  makeChart('cRadar', {
      type: 'radar',
      data: {
          labels: ['狙擊勝率', '盈虧比', '不扛單風控', '穩定度 (Win Days)', '紀律純度', '出手克制度'],
          datasets: [{
              label: '目前能力值',
              data: [scoreWinRate, scorePF, scoreRisk, scoreConsist, scoreDisc, scoreFreq],
              backgroundColor: 'rgba(56, 189, 248, 0.2)',
              borderColor: '#38bdf8',
              pointBackgroundColor: '#0ea5e9',
              pointBorderColor: '#fff',
              borderWidth: 2,
          }]
      },
      options: {
          responsive: true, maintainAspectRatio: false,
          scales: { r: { angleLines: { color: '#e2e8f0' }, grid: { color: '#e2e8f0' }, pointLabels: { font: { size: 11, weight: 'bold' } }, ticks: { display: false, min: 0, max: 100 } } },
          plugins: { legend: { display: false } }
      }
  });

  makeChart('cDashNet', {
    type:'line', data:{ labels:dLabels, datasets:[{ label:'Cumulative Net P&L ($)', data:eqArr, borderColor:'#10b981', backgroundColor:'rgba(16,185,129,0.10)', fill:true, tension:0.25, pointRadius:2 }] },
    options:{ maintainAspectRatio:false, plugins:{ legend:{display:false} } }
  });
  makeChart('cDashDaily', {
    type:'bar', data:{ labels:dLabels, datasets:[{ label:'Daily P&L ($)', data:s.days.map(d=>Number(d.net.toFixed(2))), backgroundColor:s.days.map(d=>d.net>=0?'#10b981':'#ef4444'), borderRadius:4 }] },
    options:{ maintainAspectRatio:false, plugins:{ legend:{display:false} } }
  });
  makeChart('cDashWinRate', {
    type:'bar', data:{ labels:dLabels, datasets:[
      { label:'Wins', data:s.days.map(d=>d.wins), backgroundColor:'#10b981', borderRadius:3 },
      { label:'Losses', data:s.days.map(d=>-d.losses), backgroundColor:'#ef4444', borderRadius:3 }
    ]},
    options:{ maintainAspectRatio:false, scales:{ x:{stacked:true}, y:{stacked:true} }, plugins:{ legend:{display:false} } }
  });

  document.getElementById('gapBadge').innerHTML = `<span class="tag-viol">紀律洩漏差額: ${fmtMoney(s.leak)}</span>`;
  makeChart('cDashGap', {
    type:'line', data:{ labels:dLabels, datasets:[
      { label:`鎖死單筆止損 (-1R) 影子淨值`, data:shArr, borderColor:'#10b981', borderWidth:2.2, tension:0.2, pointRadius:1.5 },
      { label:'真實累積淨值', data:eqArr, borderColor:'#2563eb', borderWidth:2.2, tension:0.2, pointRadius:1.5 }
    ]},
    options:{ maintainAspectRatio:false }
  });

  const longCnt = filtered.filter(t => t.side === 'Long').length;
  const shortCnt = filtered.filter(t => t.side === 'Short').length;
  const avgHoldW = s.wins.length ? s.wins.reduce((a,b)=>a+b.hold_sec,0)/s.wins.length : 0;
  const avgHoldL = s.losses.length ? s.losses.reduce((a,b)=>a+b.hold_sec,0)/s.losses.length : 0;
  const maxHoldW = s.wins.length ? Math.max(...s.wins.map(t=>t.hold_sec)) : 0;
  const maxHoldL = s.losses.length ? Math.max(...s.losses.map(t=>t.hold_sec)) : 0;

  const byCnt = [...s.symbols].sort((a,b)=>b.cnt-a.cnt)[0];
  const byGain = [...s.symbols].sort((a,b)=>b.net-a.net)[0];
  const byLoss = [...s.symbols].sort((a,b)=>a.net-b.net)[0];

  const kvTable = rows => `<table class="clean">${rows.map(r=>`<tr><td>${r[0]}</td><td class="val-r">${r[1]}</td></tr>`).join('')}</table>`;
  document.getElementById('dashTradesInfo').innerHTML = kvTable([
    ['Total gain/loss', fmtMoney(s.net)],
    ['Average gain/loss per trade', fmtMoney(s.net / s.n)],
    ['Average daily gain/loss', fmtMoney(s.net / s.days.length)],
    ['Total number of trades', fmtInt(s.n)],
    ['Number of long trades', fmtInt(longCnt)],
    ['Number of short trades', fmtInt(shortCnt)],
    ['Number of open trades', '0'],
    ['Number of closed trades', fmtInt(s.n)]
  ]);
  document.getElementById('dashPerfInfo').innerHTML = kvTable([
    ['Number of winning trades', fmtInt(s.wins.length)],
    ['Number of losing trades', fmtInt(s.losses.length)],
    ['Max consecutive wins', fmtInt(s.maxWStreak)],
    ['Max consecutive losses', fmtInt(s.maxLStreak)],
    ['Average hold time (winning trades)', fmtHold(avgHoldW)],
    ['Average hold time (losing trades)', fmtHold(avgHoldL)],
    ['Longest hold time (winning trades)', fmtHold(maxHoldW)],
    ['Longest hold time (losing trades)', fmtHold(maxHoldL)]
  ]);
  document.getElementById('dashSymInfo').innerHTML = kvTable([
    ['Most traded symbol', `${byCnt.symbol} (${byCnt.cnt})`],
    ['Top gainer symbol', `<span class="pos">${byGain.symbol} ${fmtMoney(byGain.net)}</span>`],
    ['Top loser symbol', `<span class="neg">${byLoss.symbol} ${fmtMoney(byLoss.net)}</span>`],
    ['Win/Loss Ratio', s.wlCount.toFixed(2)],
    ['First trading day', firstD],
    ['Last trading day', lastD],
    ['Total commissions & fees', fmtMoney(s.totalComm)]
  ]);
}

function setCalMode(m) {
  calMode = m;
  document.getElementById('btnCalMonth').className = m === 'month' ? 'btn btn-primary' : 'btn';
  document.getElementById('btnCalYear').className = m === 'year' ? 'btn btn-primary' : 'btn';
  renderCalendar();
}
function stepCalMonth(delta) {
  calMonth += delta;
  if (calMonth > 12) { calMonth = 1; calYear++; }
  if (calMonth < 1) { calMonth = 12; calYear--; }
  document.getElementById('calMonthSel').value = calMonth;
  document.getElementById('calYearSel').value = calYear;
  renderCalendar();
}
function renderCalendar() {
  calMonth = Number(document.getElementById('calMonthSel').value || calMonth);
  calYear = Number(document.getElementById('calYearSel').value || calYear);
  const dayMap = {};
  filtered.forEach(t => {
    if (!dayMap[t.date]) dayMap[t.date] = { cnt:0, net:0, shadow:0 };
    dayMap[t.date].cnt++; 
    dayMap[t.date].net += t.net_pnl;
    dayMap[t.date].shadow += t.shadow_pnl;
  });

  const mNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  const box = document.getElementById('calContainer');

  if (calMode === 'year') {
    document.getElementById('calTitleLabel').innerText = `${calYear} 年度盈虧總覽`;
    let html = '<div class="grid-4">';
    for (let m = 1; m <= 12; m++) {
      const prefix = `${calYear}-${String(m).padStart(2,'0')}`;
      const mTrades = filtered.filter(t => t.month === prefix);
      const net = mTrades.reduce((s,t)=>s+t.net_pnl, 0);
      const sh = mTrades.reduce((s,t)=>s+t.shadow_pnl, 0);
      html += `<div class="card" style="cursor:pointer;" onclick="document.getElementById('calMonthSel').value=${m};setCalMode('month')">
        <div class="sec-title"><span>${mNames[m-1]}</span><span>${mTrades.length} Trades</span></div>
        <div class="kpi-val ${net>=0?'pos':'neg'}">${fmtMoney(net)}</div>
        <div class="kpi-sub">機械 -1R 影子淨利: ${fmtMoney(sh)}</div>
      </div>`;
    }
    box.innerHTML = html + '</div>';
    return;
  }

  const prefix = `${calYear}-${String(calMonth).padStart(2,'0')}`;
  const mNet = filtered.filter(t => t.month === prefix).reduce((s,t)=>s+t.net_pnl, 0);
  document.getElementById('calTitleLabel').innerHTML = `${mNames[calMonth-1]} ${calYear} &nbsp;<span class="${mNet>=0?'pos':'neg'}" style="font-size:15px;">(${fmtMoney(mNet, true)})</span>`;

  // Update headers to include Weekly P&L
  const dows = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun', 'Weekly P&L'];
  let html = '<div class="cal-grid" style="grid-template-columns:repeat(7,1fr) minmax(110px, 1fr);">' + dows.map(d=>`<div class="cal-dow">${d}</div>`).join('');

  const firstDay = new Date(calYear, calMonth - 1, 1);
  // getDay() returns 0 for Sunday. We want Monday=0, Sunday=6
  const startOffset = (firstDay.getDay() + 6) % 7; 
  const daysInMonth = new Date(calYear, calMonth, 0).getDate();

  let weekNet = 0;
  let weekTrades = 0;
  let weekActiveDays = 0;
  let currentCol = 0;
  let weekNum = 1;

  // Fill initial empty cells
  for (let i = 0; i < startOffset; i++) {
      html += '<div class="cal-cell muted"></div>';
      currentCol++;
  }

  for (let d = 1; d <= daysInMonth; d++) {
    const dStr = `${calYear}-${String(calMonth).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const info = dayMap[dStr];
    let inner = '';
    
    if (info) {
      const cls = info.net >= 0 ? 'win' : 'loss';
      inner = `<div class="cal-box ${cls}" onclick="openNoteForDate('${dStr}')" style="display:flex; flex-direction:column; justify-content:space-between; height:100%;">
        <div class="t-cnt" style="font-size:11px; font-weight:600; opacity:0.75; margin-bottom:4px;">${info.cnt} Trade${info.cnt > 1 ? 's' : ''}</div>
        <div class="t-pnl" style="font-size:15px; font-weight:800;">${fmtMoney(info.net)}</div>
      </div>`;
      weekNet += info.net;
      weekTrades += info.cnt;
      weekActiveDays += 1;
    }
    
    html += `<div class="cal-cell"><div class="cal-daynum">${d}</div>${inner}</div>`;
    currentCol++;

    // End of the week (Sunday)
    if (currentCol === 7) {
        const wCls = weekNet >= 0 ? 'pos' : 'neg';
        const displayNet = weekNet === 0 ? '-' : fmtMoney(weekNet);
        html += `<div class="cal-cell" style="background:#f8fafc; align-items:center; justify-content:center; text-align:center; border-left:2px solid var(--border);">
            <div style="font-size:11.5px; color:var(--muted); font-weight:700; margin-bottom:4px;">Week ${weekNum}</div>
            <div style="font-size:16px; font-weight:800;" class="${wCls}">${displayNet}</div>
            <div style="font-size:11px; color:var(--muted); margin-top:6px;">${weekActiveDays} active days</div>
        </div>`;
        
        currentCol = 0;
        weekNet = 0;
        weekTrades = 0;
        weekActiveDays = 0;
        weekNum++;
    }
  }
  
  // Fill remaining empty cells for the last week
  if (currentCol > 0) {
      const rem = 7 - currentCol;
      for (let i = 0; i < rem; i++) {
          html += '<div class="cal-cell muted"></div>';
      }
      // Add the final week's summary cell
      const wCls = weekNet >= 0 ? 'pos' : 'neg';
      const displayNet = weekNet === 0 ? '-' : fmtMoney(weekNet);
      html += `<div class="cal-cell" style="background:#f8fafc; align-items:center; justify-content:center; text-align:center; border-left:2px solid var(--border);">
          <div style="font-size:11.5px; color:var(--muted); font-weight:700; margin-bottom:4px;">Week ${weekNum}</div>
          <div style="font-size:16px; font-weight:800;" class="${wCls}">${displayNet}</div>
          <div style="font-size:11px; color:var(--muted); margin-top:6px;">${weekActiveDays} active days</div>
      </div>`;
  }
  
  box.innerHTML = html + '</div>';
}

function switchStatTab(tab, btn) {
  activeStatTab = tab;
  document.querySelectorAll('.subtab').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  renderStatistics(computeCoreStats(filtered));
}

function renderStatistics(s) {
  const el = document.getElementById('statTabContent');
  if (!s.n) { el.innerHTML = '<div class="card">無交易數據</div>'; return; }
  const mCnt = Math.max(1, s.months.length), yCnt = Math.max(1, s.years.length), dCnt = Math.max(1, s.days.length);
  const sCard = (title, sub, val, cls='') => `<div class="card stat-card"><div class="kpi-lbl">${title}</div><div class="kpi-sub">${sub}</div><div class="kpi-val ${cls}">${val}</div></div>`;

  if (activeStatTab === 'pnl') {
    const maxDay = Math.max(...s.days.map(d=>d.net)), minDay = Math.min(...s.days.map(d=>d.net));
    const maxMo = Math.max(...s.months.map(m=>m.net)), minMo = Math.min(...s.months.map(m=>m.net));
    const maxYr = Math.max(...s.years.map(y=>y.net)), minYr = Math.min(...s.years.map(y=>y.net));
    el.innerHTML = `
      <div class="grid-3">
        ${sCard('P&L Total','Net Profit & Loss',fmtMoney(s.net),s.net>=0?'pos':'neg')}
        ${sCard('Average P&L / Trade','Average Net Profit & Loss',fmtMoney(s.net/s.n),(s.net/s.n)>=0?'pos':'neg')}
        ${sCard('Average P&L / Day','Average Net Profit & Loss',fmtMoney(s.net/dCnt),(s.net/dCnt)>=0?'pos':'neg')}
        ${sCard('Average P&L / Month','Average Net Profit & Loss',fmtMoney(s.net/mCnt),(s.net/mCnt)>=0?'pos':'neg')}
        ${sCard('Average P&L / Year','Average Net Profit & Loss',fmtMoney(s.net/yCnt),(s.net/yCnt)>=0?'pos':'neg')}
        ${sCard('Max P&L / Trade','Highest Net Profit & Loss',fmtMoney(s.maxWin.net_pnl),'pos')}
        ${sCard('Min P&L / Trade','Lowest Net Profit & Loss',fmtMoney(s.maxLoss.net_pnl),'neg')}
        ${sCard('Max P&L / Day','Highest Net Profit & Loss',fmtMoney(maxDay),'pos')}
        ${sCard('Min P&L / Day','Lowest Net Profit & Loss',fmtMoney(minDay),'neg')}
        ${sCard('Max P&L / Month','Highest Net Profit & Loss',fmtMoney(maxMo),'pos')}
        ${sCard('Min P&L / Month','Lowest Net Profit & Loss',fmtMoney(minMo),'neg')}
        ${sCard('Max P&L / Year','Highest Net Profit & Loss',fmtMoney(maxYr),'pos')}
        ${sCard('Min P&L / Year','Lowest Net Profit & Loss',fmtMoney(minYr),minYr>=0?'pos':'neg')}
        ${sCard('Mechanical -1R Net P&L', '鎖死單筆止損 (-1R) 影子淨利', fmtMoney(s.shadowNet), 'pos')}
        ${sCard('Discipline Leak Cost','攤平與退止損洩漏總成本',fmtMoney(s.leak),'neg')}
      </div>
      <div class="grid-2">
        <div class="card"><div class="sec-title">Daily P&L</div><div class="chart-wrap"><canvas id="cStatDaily"></canvas></div></div>
        <div class="card"><div class="sec-title">Net P&L</div><div class="chart-wrap"><canvas id="cStatNet"></canvas></div></div>
      </div>`;
    let cum = 0;
    makeChart('cStatDaily', { type:'bar', data:{ labels:s.days.map(d=>d.date.slice(5)), datasets:[{ data:s.days.map(d=>d.net), backgroundColor:s.days.map(d=>d.net>=0?'#10b981':'#ef4444') }] }, options:{ maintainAspectRatio:false, plugins:{legend:{display:false}} } });
    makeChart('cStatNet', { type:'line', data:{ labels:s.days.map(d=>d.date.slice(5)), datasets:[{ data:s.days.map(d=>(cum+=d.net, Number(cum.toFixed(2)))), borderColor:'#10b981', backgroundColor:'rgba(16,185,129,0.1)', fill:true, tension:0.2 }] }, options:{ maintainAspectRatio:false, plugins:{legend:{display:false}} } });
  }
  else if (activeStatTab === 'trades') {
    const avgHold = filtered.reduce((a,b)=>a+b.hold_sec,0)/s.n;
    const maxHold = Math.max(...filtered.map(t=>t.hold_sec)), minHold = Math.min(...filtered.map(t=>t.hold_sec));
    const maxEx = Math.max(...filtered.map(t=>t.execs)), minEx = Math.min(...filtered.map(t=>t.execs));
    el.innerHTML = `
      <div class="grid-3">
        ${sCard('Total Number of Trades','Closed Round-Trips',fmtInt(s.n))}
        ${sCard('Total Number of Open Trades','Currently Open','0')}
        ${sCard('Total Number of Executed Trades','Total Broker Fills',fmtInt(s.totalExecs))}
        ${sCard('Average Trades per Month','Monthly Frequency',(s.n/mCnt).toFixed(2))}
        ${sCard('Total Number of Closed Trades','Completed Trades',fmtInt(s.n))}
        ${sCard('Average Executions per Trade','Fills / Round-Trip',(s.totalExecs/s.n).toFixed(2))}
        ${sCard('Average Trades per Year','Annualized Pace',(s.n/yCnt).toFixed(2))}
        ${sCard('Total Number of Long Trades','Long Positions',fmtInt(filtered.filter(t=>t.side==='Long').length))}
        ${sCard('Max Executions / Trade','Highest Fills in 1 Trade',fmtInt(maxEx))}
        ${sCard('Total Duration','Active Span',`${mCnt} months`)}
        ${sCard('Total Number of Short Trades','Short Positions',fmtInt(filtered.filter(t=>t.side==='Short').length))}
        ${sCard('Min Executions / Trade','Lowest Fills in 1 Trade',fmtInt(minEx))}
      </div>
      <div class="grid-3">
        <div style="grid-column:span 2;" class="grid-2">
          ${sCard('Average Trade Duration','Mean Hold Time',fmtHold(avgHold))}
          ${sCard('Total Number of Win Trades','Winning Trades',fmtInt(s.wins.length),'pos')}
          ${sCard('Longest Trade','Max Hold Time',fmtHold(maxHold))}
          ${sCard('Total Number of Loss Trades','Losing Trades',fmtInt(s.losses.length),'neg')}
          ${sCard('Fastest Trade','Shortest Hold Time',fmtHold(minHold))}
          ${sCard('Breakeven Total','Net Zero Trades',fmtInt(s.bes.length))}
        </div>
        <div class="card" style="display:flex;flex-direction:column;align-items:center;justify-content:center;">
          <div class="sec-title">Win / Loss Ratio (${s.wlCount.toFixed(2)})</div>
          <div style="height:210px;width:210px;"><canvas id="cStatDonut"></canvas></div>
        </div>
      </div>`;
    makeChart('cStatDonut', { type:'doughnut', data:{ labels:[`Wins (${s.wins.length})`,`Losses (${s.losses.length})`], datasets:[{ data:[s.wins.length, s.losses.length], backgroundColor:['#10b981','#f87171'] }] }, options:{ maintainAspectRatio:false } });
  }
  else if (activeStatTab === 'symbol') {
    const mostTraded = [...s.symbols].sort((a,b)=>b.cnt-a.cnt)[0];
    const highVol = [...s.symbols].sort((a,b)=>b.vol-a.vol)[0];
    const mostWin = [...s.symbols].sort((a,b)=>b.wins-a.wins)[0];
    const mostLoss = [...s.symbols].sort((a,b)=>b.losses-a.losses)[0];
    const topGain = [...s.symbols].sort((a,b)=>b.net-a.net)[0];
    const topLoss = [...s.symbols].sort((a,b)=>a.net-b.net)[0];
    const symBox = (title, sym, subLbl, val, cls='') => `<div class="card stat-card"><div class="kpi-lbl" style="font-size:15px;">${title}</div><div class="kpi-sub">Symbol</div><div><span class="sym-pill">${sym}</span></div><div class="kpi-sub">${subLbl}</div><div class="kpi-val ${cls}">${val}</div></div>`;
    const rows = [...s.symbols].sort((a,b)=>b.net-a.net).map(x => `<tr><td><span class="sym-pill" style="margin:0;">${x.symbol}</span></td><td>${x.cnt}</td><td>${(x.wins/x.cnt*100).toFixed(1)}%</td><td>${fmtInt(x.vol)}</td><td class="${x.net>=0?'pos':'neg'}" style="font-weight:700;">${fmtMoney(x.net)}</td><td>${x.viols>0?`<span class="tag-viol">${x.viols} 次違規</span>`:'<span class="tag-disc">OK</span>'}</td></tr>`).join('');
    el.innerHTML = `
      <div class="grid-3">
        ${symBox('Most Traded Symbol', mostTraded.symbol, 'Trades', fmtInt(mostTraded.cnt))}
        ${symBox('Highest Traded Volume Symbol', highVol.symbol, 'Shares Volume', fmtInt(highVol.vol))}
        ${symBox('Most Winning Trades Symbol', mostWin.symbol, 'Winning Trades', fmtInt(mostWin.wins))}
        ${symBox('Most Losing Trades Symbol', mostLoss.symbol, 'Losing Trades', fmtInt(mostLoss.losses))}
        ${symBox('Top Gainer Symbol', topGain.symbol, 'Net P&L', fmtMoney(topGain.net), 'pos')}
        ${symBox('Top Loser Symbol', topLoss.symbol, 'Net P&L', fmtMoney(topLoss.net), 'neg')}
      </div>
      <div class="card"><div class="sec-title">All Traded Symbols Breakdown</div>
        <table class="clean"><thead><tr><th>Symbol</th><th>Trades</th><th>Win %</th><th>Total Volume</th><th>Net P&L</th><th>Discipline</th></tr></thead><tbody>${rows}</tbody></table>
      </div>`;
  }
  else if (activeStatTab === 'volume') {
    const maxV = Math.max(...filtered.map(t=>t.volume)), minV = Math.min(...filtered.map(t=>t.volume));
    el.innerHTML = `
      <div class="grid-3">
        ${sCard('Total Volume','雙邊總成交股數',fmtInt(s.totalVol))}
        ${sCard('Total Closed Volume','已平倉總成交股數',fmtInt(s.totalVol))}
        ${sCard('Total Open Volume','未平倉股數','0')}
        ${sCard('Total Winning Volume','獲利單總股數',fmtInt(s.winVol))}
        ${sCard('Total Losing Volume','虧損單總股數',fmtInt(s.lossVol))}
        ${sCard('Max Traded Volume','單筆最大雙邊股數',fmtInt(maxV))}
        ${sCard('Average Volume per Trade','每筆平均雙邊股數',(s.totalVol/s.n).toFixed(2))}
        ${sCard('Average Winning Volume Traded','獲利單平均雙邊股數',(s.wins.length?s.winVol/s.wins.length:0).toFixed(2))}
        ${sCard('Average Losing Volume Traded','虧損單平均雙邊股數',(s.losses.length?s.lossVol/s.losses.length:0).toFixed(2))}
        ${sCard('Average Volume per Day','每日平均成交股數',(s.totalVol/dCnt).toFixed(2))}
        ${sCard('Average Traded Volume per Month','每月平均成交股數',(s.totalVol/mCnt).toFixed(2))}
        ${sCard('Min Traded Volume','單筆最小雙邊股數',fmtInt(minV))}
      </div>
      <div class="card"><div class="sec-title">Daily Volume</div><div class="chart-wrap"><canvas id="cStatVol"></canvas></div></div>`;
    makeChart('cStatVol', { type:'bar', data:{ labels:s.days.map(d=>d.date.slice(5)), datasets:[{ label:'Daily Volume', data:s.days.map(d=>d.vol), backgroundColor:'#38bdf8' }] }, options:{ maintainAspectRatio:false, plugins:{legend:{display:false}} } });
  }
  else if (activeStatTab === 'summary') {
    const avgHold = filtered.reduce((a,b)=>a+b.hold_sec,0)/s.n;
    el.innerHTML = `
      <div class="grid-3">
        ${sCard('Total Number of Trades', 'Closed Round-Trips', fmtInt(s.n))}
        ${sCard('Total Number of Win Trades', 'Winning Trades', fmtInt(s.wins.length), 'pos')}
        ${sCard('Total Number of Loss Trades', 'Losing Trades', fmtInt(s.losses.length), 'neg')}
        ${sCard('Winning Percentage', 'Win %', `${s.winPct.toFixed(2)}%`)}
        ${sCard('Average Winner', 'Average Gain', fmtMoney(s.avgWin), 'pos')}
        ${sCard('Average Loser', 'Average Loss', fmtMoney(s.avgLoss), 'neg')}
        ${sCard('Average Trades per Day', 'Daily Frequency', s.avgTradesDay.toFixed(2))}
        ${sCard('Win / Loss Ratio', 'W/L Ratio', s.wlCount.toFixed(2))}
        ${sCard('Average Executions Per Trade', 'Fills per Trade', (s.totalExecs/s.n).toFixed(2))}
        ${sCard('Long Trades', 'Long Positions', fmtInt(filtered.filter(t=>t.side==='Long').length))}
        ${sCard('Short Trades', 'Short Positions', fmtInt(filtered.filter(t=>t.side==='Short').length))}
        ${sCard('Average Trade Duration', 'Mean Hold Time', fmtHold(avgHold))}
        ${sCard('Net P&L (Actual)', 'Realized Net Profit', fmtMoney(s.net), s.net>=0?'pos':'neg')}
        ${sCard('Mechanical -1R Shadow Net', 'Shadow P&L (-1R Cap)', fmtMoney(s.shadowNet), s.shadowNet>=0?'pos':'neg')}
        ${sCard('Discipline Leak Cost', '攤平/退止損代價', fmtMoney(s.leak), 'neg')}
        ${sCard('Added to Loser Count', '逆勢攤平次數', `${filtered.filter(t=>t.added_to_loser).length} 次`, 'neg')}
        ${sCard('Exceeded -1R Count', '超額虧損次數', `${filtered.filter(t=>t.exceeded_1r).length} 次`, 'neg')}
        ${sCard('Average Net P&L per Trade', 'Expectancy per Trade', fmtMoney(s.net/s.n), (s.net/s.n)>=0?'pos':'neg')}
      </div>
    `;
  }
  else if (activeStatTab === 'session') {
    const sessKeys = ["1. Pre-Market (<09:30)","2. Open 30m (09:30-10:00)","3. Morning (10:00-10:30)","4. Late Morn (10:30-11:00)","5. Midday (11:00-12:00)","6. Afternoon (12:00+)"];
    const sessData = sessKeys.map(k => {
      const arr = filtered.filter(t => t.us_session === k);
      return { k, cnt:arr.length, net:arr.reduce((a,b)=>a+b.net_pnl,0), sh:arr.reduce((a,b)=>a+b.shadow_pnl,0) };
    });
    const holdBuckets = [
      ['< 1 min (極短線)', t => t.hold_sec < 60],
      ['1 - 5 mins', t => t.hold_sec >= 60 && t.hold_sec < 300],
      ['5 - 15 mins', t => t.hold_sec >= 300 && t.hold_sec < 900],
      ['15 - 60 mins', t => t.hold_sec >= 900 && t.hold_sec < 3600],
      ['> 60 mins (死扛高危區)', t => t.hold_sec >= 3600]
    ].map(([lbl, fn]) => {
      const arr = filtered.filter(fn);
      return { lbl, cnt:arr.length, winPct:arr.length?(arr.filter(x=>x.net_pnl>0).length/arr.length*100):0, net:arr.reduce((a,b)=>a+b.net_pnl,0) };
    });

    // --- AI Coach Auto-Analysis Generation ---
    // 1. Time of Day Analysis
    const bestSess = [...sessData].sort((a,b) => b.net - a.net)[0];
    const worstSess = [...sessData].sort((a,b) => a.net - b.net)[0];
    let timeAdvice = `你在 <b>${bestSess.k.split(' ')[1]}</b> 時段表現最好，淨利高達 <span class="pos">${fmtMoney(bestSess.net)}</span>，這是你的黃金印鈔時間。`;
    if (worstSess.net < 0) {
        const leakDiff = worstSess.sh - worstSess.net;
        timeAdvice += `<br>⚠️ <b>危險區間：</b> ${worstSess.k.split(' ')[1]} 時段你虧損了 <span class="neg">${fmtMoney(worstSess.net)}</span>。更嚴重的是，如果這時段你嚴格執行 -1R 停損，原本可以保住 ${fmtMoney(leakDiff)} 的利潤洩漏！建議 10:00 AM 後直接關閉電腦。`;
    }

    // 2. Hold Duration Analysis
    const bestHold = [...holdBuckets].sort((a,b) => b.net - a.net)[0];
    const worstHold = [...holdBuckets].sort((a,b) => a.net - b.net)[0];
    let holdAdvice = `你的獲利絕大多數來自於 <b>${bestHold.lbl}</b> 的交易，勝率高達 ${bestHold.winPct.toFixed(1)}%。這說明你的優勢在於「動能爆發 (Momentum Scalp)」，而非長線波段。`;
    if (worstHold.net < 0) {
        holdAdvice += `<br>🚨 <b>致命盲點：</b> 當你持倉時間落入 <b>${worstHold.lbl}</b>，勝率暴跌至 ${worstHold.winPct.toFixed(1)}%，並造成了 <span class="neg">${fmtMoney(worstHold.net)}</span> 的巨大坑洞。這證明了「扛單等待反彈」對你的策略完全無效，超時必須無條件平倉。`;
    }

    const aiCoachHtml = `
      <div style="background:#f0f9ff; border:1px solid #bae6fd; border-radius:12px; padding:18px 24px; margin-bottom:20px; box-shadow:0 2px 4px rgba(0,0,0,0.02);">
        <div style="display:flex; align-items:center; gap:10px; margin-bottom:12px;">
            <div style="background:#0ea5e9; color:#fff; width:28px; height:28px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:14px;">🤖</div>
            <h3 style="margin:0; font-size:16px; color:#0369a1; font-weight:800;">AI 交易教練：數據深度診斷 (Auto-Insights)</h3>
        </div>
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px; font-size:13.5px; color:#334155; line-height:1.6;">
            <div style="background:#fff; padding:16px; border-radius:8px; border:1px solid #e0f2fe;">
                <div style="font-weight:800; color:#0284c7; margin-bottom:8px;">🕒 時段優勢 (Session Edge)</div>
                ${timeAdvice}
            </div>
            <div style="background:#fff; padding:16px; border-radius:8px; border:1px solid #e0f2fe;">
                <div style="font-weight:800; color:#0284c7; margin-bottom:8px;">⏱️ 持倉耐心 (Hold Duration)</div>
                ${holdAdvice}
            </div>
        </div>
      </div>
    `;

    el.innerHTML = aiCoachHtml + `
      <div class="grid-2">
        <div class="card"><div class="sec-title">美東開盤時段盈虧分佈 (US Eastern Session P&L)</div><div class="chart-wrap"><canvas id="cSessBar"></canvas></div></div>
        <div class="card"><div class="sec-title">Per-Share 損益散點圖 ($/Share，紅點為違規單)</div><div class="chart-wrap"><canvas id="cPsScatter"></canvas></div></div>
      </div>
      <div class="grid-2">
        <div class="card"><div class="sec-title">各時段真實淨利 vs. 機械 -1R 影子淨利</div>
          <table class="clean"><thead><tr><th>美東時段</th><th>筆數</th><th>真實淨利</th><th>機械 -1R 淨利</th></tr></thead>
          <tbody>${sessData.map(x=>`<tr><td>${x.k}</td><td>${x.cnt}</td><td class="${x.net>=0?'pos':'neg'}" style="font-weight:700;">${fmtMoney(x.net)}</td><td class="pos">${fmtMoney(x.sh)}</td></tr>`).join('')}</tbody></table>
        </div>
        <div class="card"><div class="sec-title">持倉時間區間表現 (Hold Duration Breakdown)</div>
          <table class="clean"><thead><tr><th>持倉時間</th><th>筆數</th><th>勝率</th><th>真實淨利</th></tr></thead>
          <tbody>${holdBuckets.map(x=>`<tr><td>${x.lbl}</td><td>${x.cnt}</td><td>${x.winPct.toFixed(1)}%</td><td class="${x.net>=0?'pos':'neg'}" style="font-weight:700;">${fmtMoney(x.net)}</td></tr>`).join('')}</tbody></table>
        </div>
      </div>`;
    makeChart('cSessBar', { type:'bar', data:{ labels:sessData.map(x=>x.k.slice(3)), datasets:[{ label:'Actual Net P&L ($)', data:sessData.map(x=>Number(x.net.toFixed(2))), backgroundColor:sessData.map(x=>x.net>=0?'#10b981':'#ef4444') }] }, options:{ maintainAspectRatio:false, plugins:{legend:{display:false}} } });
    makeChart('cPsScatter', { type:'scatter', data:{ datasets:[{ label:'$/Share', data:filtered.map((t,i)=>({x:i+1,y:t.pnl_ps})), backgroundColor:filtered.map(t=>t.disciplined?'#2563eb':'#ef4444'), pointRadius:4.5 }] }, options:{ maintainAspectRatio:false, plugins:{legend:{display:false}} } });
  }
}

function loadSimDefaults(useMechanicalCap=false) {
  const s = computeCoreStats(filtered);
  document.getElementById('simStart').value = PAYLOAD.start_equity;
  if (!s.n) return;
  const mCnt = Math.max(1, s.months.length);
  document.getElementById('simWinPct').value = s.winPct.toFixed(2);
  document.getElementById('simTradesMo').value = Math.round(s.n / mCnt);
  if (useMechanicalCap) {
    const shLosses = filtered.filter(t => t.shadow_pnl < 0);
    const avgShLoss = shLosses.length ? Math.abs(shLosses.reduce((a,b)=>a+b.shadow_pnl,0)/shLosses.length) : ONE_R;
    const wl = avgShLoss > 0 ? (s.avgWin / avgShLoss) : 1.0;
    document.getElementById('simWlRatio').value = wl.toFixed(2);
    document.getElementById('simRisk').value = avgShLoss.toFixed(2);
  } else {
    document.getElementById('simWlRatio').value = s.wlDollar.toFixed(2);
    document.getElementById('simRisk').value = Math.abs(s.avgLoss || ONE_R).toFixed(2);
  }
  runMonteCarlo();
}

function runMonteCarlo() {
  const startEq = Number(document.getElementById('simStart').value || 12500);
  const winP = Number(document.getElementById('simWinPct').value || 50) / 100;
  const wlRatio = Number(document.getElementById('simWlRatio').value || 1);
  const trPerMo = Math.max(1, Math.round(Number(document.getElementById('simTradesMo').value || 30)));
  const risk = Number(document.getElementById('simRisk').value || 50);

  const scenarios = [];
  for (let k = 0; k < 20; k++) {
    let eq = startEq;
    const path = [startEq];
    for (let m = 1; m <= 12; m++) {
      for (let t = 0; t < trPerMo; t++) { eq += (Math.random() < winP) ? (risk * wlRatio) : (-risk); }
      path.push(Number(eq.toFixed(2)));
    }
    scenarios.push(path);
  }
  scenarios.sort((a,b) => a[12] - b[12]);
  const worst = scenarios[0][12], best = scenarios[19][12], medianPath = scenarios[10], medFinal = medianPath[12];
  const retPct = startEq > 0 ? ((medFinal - startEq) / startEq * 100) : 0;

  document.getElementById('simSummaryBar').innerHTML = `
    <div><div class="kpi-lbl">Starting Equity</div><div style="font-weight:800;">${fmtMoney(startEq)}</div></div>
    <div><div class="kpi-lbl">Median Final Equity</div><div class="${medFinal>=startEq?'pos':'neg'}" style="font-weight:800;">${fmtMoney(medFinal)}</div></div>
    <div><div class="kpi-lbl">Return</div><div class="${retPct>=0?'pos':'neg'}" style="font-weight:800;">${retPct.toFixed(2)}%</div></div>
    <div><div class="kpi-lbl">Best Case</div><div class="pos" style="font-weight:800;">${fmtMoney(best)}</div></div>
    <div><div class="kpi-lbl">Worst Case</div><div class="${worst>=startEq?'pos':'neg'}" style="font-weight:800;">${fmtMoney(worst)}</div></div>
  `;
  const labels = Array.from({length:13}, (_,i)=>`Month ${i}`);
  const datasets = scenarios.map((p, idx) => idx === 10 ? {
    label:'Median Scenario', data:p, borderColor: medFinal >= startEq ? '#10b981' : '#ef4444', borderWidth:3.2, pointRadius:3, tension:0.25, order:1
  } : {
    label:`Sim ${idx+1}`, data:p, borderColor:'rgba(148,163,184,0.35)', borderWidth:1.1, pointRadius:0, tension:0.25, order:2
  });
  makeChart('cMonteCarlo', { type:'line', data:{ labels, datasets }, options:{ maintainAspectRatio:false, plugins:{ legend:{ labels:{ filter: item => item.text === 'Median Scenario' } } } } });
}

function setSort(colKey) {
  if (sortCol === colKey) {
    if (sortAsc) sortAsc = false; else { sortCol = null; sortAsc = true; } 
  } else { sortCol = colKey; sortAsc = true; }
  tradesPage = 1;
  renderTradesPage();
}

function renderTradesPage() {
  const pageSize = Number(document.getElementById('tradesPageSize').value || 20);
  let sortedArr = [...filtered].reverse(); 

  if (sortCol) {
    sortedArr.sort((a, b) => {
      let vA = a[sortCol], vB = b[sortCol];
      if (typeof vA === 'string') { vA = vA.toLowerCase(); vB = vB.toLowerCase(); }
      if (vA < vB) return sortAsc ? -1 : 1;
      if (vA > vB) return sortAsc ? 1 : -1;
      return 0;
    });
  }

  const totalPages = Math.max(1, Math.ceil(sortedArr.length / pageSize));
  if (tradesPage > totalPages) tradesPage = totalPages;
  const slice = sortedArr.slice((tradesPage - 1) * pageSize, tradesPage * pageSize);

  document.getElementById('tradesCountLabel').innerText = `Trades (共 ${sortedArr.length} 筆平倉紀錄)`;

  const th = (lbl, key) => {
    let cls = '';
    if (sortCol === key) cls = sortAsc ? 'class="sorted-asc"' : 'class="sorted-desc"';
    return `<th ${cls} onclick="setSort('${key}')" title="Click to sort ascending, click again to sort descending">${lbl}</th>`;
  };

  let html = `<thead><tr>
    <th>Status</th><th>Asset</th>
    ${th('Symbol', 'symbol')} ${th('Side', 'side')} ${th('Open Time', 'open_time')} ${th('Close Time', 'close_time')}
    ${th('Hold', 'hold_sec')} ${th('Open Price', 'open_price')} ${th('Close Price', 'close_price')}
    ${th('Total Volume', 'volume')} ${th('Commission', 'comm')} ${th('Profit and Loss (P&L)', 'net_pnl')}
    ${th('# Executions', 'execs')} <th>Tags</th>
  </tr></thead><tbody>`;

  slice.forEach(t => {
    const pCls = t.net_pnl > 0 ? 'win' : (t.net_pnl < 0 ? 'loss' : 'flat');
    const tagHtml = t.disciplined ? `<span class="tag-disc">Disciplined</span>` : `<span class="tag-viol">${t.tags}</span>`;
    html += `<tr>
      <td><span class="badge-closed">CLOSED</span></td><td>Stock</td>
      <td style="font-weight:700;">${t.symbol}</td><td>${t.side}</td>
      <td>${t.open_time}</td><td>${t.close_time}</td><td>${fmtHoldShort(t.hold_sec)}</td>
      <td>$${t.open_price.toFixed(2)}</td><td>$${t.close_price.toFixed(2)}</td>
      <td>${fmtInt(t.volume)}</td><td>$${t.comm.toFixed(2)}</td>
      <td><span class="pnl-pill ${pCls}">${fmtMoney(t.net_pnl)}</span></td>
      <td>${t.execs}</td><td>${tagHtml}</td>
    </tr>`;
  });
  document.getElementById('tradesFullTable').innerHTML = html + '</tbody>';

  let pg = `<button class="btn" onclick="if(tradesPage>1){tradesPage--;renderTradesPage();}">◀ Prev</button>`;
  pg += `<span style="font-size:12.5px;color:var(--muted);">Page ${tradesPage} of ${totalPages}</span>`;
  pg += `<button class="btn" onclick="if(tradesPage<${totalPages}){tradesPage++;renderTradesPage();}">Next ▶</button>`;
  document.getElementById('tradesPagination').innerHTML = pg;
}

// === 同步渲染 Daily Notes 與盤前清單 ===
function renderDailyNotes() {
  const s = computeCoreStats(filtered);
  let days = [...s.days].reverse();
  if (singleNoteDate) days = days.filter(d => d.date === singleNoteDate);

  const pageSize = 8;
  const totalPages = Math.max(1, Math.ceil(days.length / pageSize));
  if (notesPage > totalPages) notesPage = totalPages;
  const slice = days.slice((notesPage - 1) * pageSize, notesPage * pageSize);
  const notes = JSON.parse(localStorage.getItem('trading_daily_notes_open') || '{}');
  const container = document.getElementById('dailyNotesContainer');

  if (!slice.length) { container.innerHTML = '<div class="card">無該日期的交易紀錄</div>'; document.getElementById('notesPagination').innerHTML=''; return; }

  container.innerHTML = slice.map((d, idx) => {
    const data = notes[d.date];
    let noteTxt = '';
    const gradeColors = {'A': '#10b981', 'B': '#f59e0b', 'C': '#ef4444', '-': '#94a3b8'};
    const gradeLbl = data && data.grade ? data.grade : '-';
    if (data && (data.text || data.q1 || data.q2 || data.q3)) {
        noteTxt = `<div class="rendered-note">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;"><div style="font-size:22px; font-weight:900; color:${gradeColors[gradeLbl]}; border:3px solid ${gradeColors[gradeLbl]}; border-radius:50%; width:42px; height:42px; display:flex; align-items:center; justify-content:center; box-shadow:0 2px 4px rgba(0,0,0,0.1);">${gradeLbl}</div></div><div class="rendered-q">1. 更有選擇性 (Be Selective): <b>${data.q1 ? '✅ 是' : '❌ 否'}</b></div>
            <div class="rendered-q">2. 執行力 (Execute): <b>${data.q2 ? '✅ 是' : '❌ 否'}</b></div>
            <div class="rendered-q">3. 持倉更久 (Hold Longer): <b>${data.q3 ? '✅ 是' : '❌ 否'}</b></div>
            <hr style="border:0; border-top:1px dashed #f59e0b; margin:10px 0;">
            <div style="white-space:pre-wrap; font-size:13px; color:#1e293b;">${data.text}</div>
        </div>`;
    }

    // 完美整合盤前工作台生成的 premarket_watchlist_db.json
    let pmHtml = '';
    const pmData = PM_DB[d.date];
    if (pmData && pmData.rows && pmData.rows.length) {
        const passed = pmData.checks ? pmData.checks.filter(Boolean).length : 0;
        const pct = (passed / 17 * 100).toFixed(1);
        const wls = pmData.rows.map(r => r.ticker).join(", ");
        pmHtml = `<div class="pm-box">
            <div class="pm-title"><span>盤前 Watchlist (${pmData.rows.length} 檔)</span><span>SOP Score: ${pct}%</span></div>
            <div style="font-size:13.5px; margin-bottom:8px;">${wls}</div>
            <div style="font-size:12px; color:var(--forest);">Edge Check: <b>${pmData.edge_check || '未記錄'}</b> | Violations: <b>${pmData.violations || '無'}</b></div>
        </div>`;
    }

    setTimeout(() => {
        getImagesForDate(d.date, function(images) {
            const box = document.getElementById('imgs_' + d.date);
            if (box) box.innerHTML = images.map(img => `<img src="${img.data}" style="max-width:48%; cursor:pointer;" onclick="window.open('${img.data}')">`).join('');
        });
        let cum = 0;
        const pts = [0], lbls = ['Start'];
        d.trades.forEach(t => { cum += t.net_pnl; pts.push(Number(cum.toFixed(2))); lbls.push(t.close_hm.slice(0,5)); });
        makeChart(`cNote_${idx}`, {
          type:'line',
          data:{ labels:lbls, datasets:[{ data:pts, borderWidth:2, pointRadius:2, tension:0.2, borderColor:d.net>=0?'#10b981':'#ef4444' }]},
          options:{ maintainAspectRatio:false, plugins:{legend:{display:false}}, scales:{ x:{ticks:{maxTicksLimit:6,font:{size:10}}}, y:{ticks:{font:{size:10}}} } }
        });
    }, 50);

    const tRows = d.trades.map(t => {
      const pCls = t.net_pnl > 0 ? 'win' : (t.net_pnl < 0 ? 'loss' : 'flat');
      const setup_label = t.disciplined ? "A 級策略" : "B 級策略";
      return `<tr><td><span class="badge-closed">CLOSED</span></td><td><b>${t.symbol}</b></td><td>${t.side}</td><td><span style="background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:11px;">${setup_label}</span></td><td>${t.open_time}</td><td>$${t.open_price.toFixed(2)}</td><td><span class="pnl-pill ${pCls}">${fmtMoney(t.net_pnl)}</span></td></tr>`;
    }).join('');

    return `<div class="card day-card">
      <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:10px;">
        <div style="font-size:16px;font-weight:800;">${d.date}</div>
        <button class="btn" onclick="editDayNote('${d.date}')">✏️ 寫覆盤與貼截圖</button>
      </div>
      <div class="day-top">
        <div><div class="kpi-lbl">Net P&L</div><div class="kpi-val ${d.net>=0?'pos':'neg'}">${fmtMoney(d.net)}</div></div>
        <div><div class="kpi-lbl">Total Trades</div><div class="kpi-val">${d.trades.length}</div></div>
        <div style="font-size:13px;line-height:2;">Winners: <b class="pos">${d.wins}</b><br>Losers: <b class="neg">${d.losses}</b></div>
        <div style="height:100px;"><canvas id="cNote_${idx}"></canvas></div>
      </div>
      ${pmHtml}
      ${noteTxt}
      <div class="saved-images-container" id="imgs_${d.date}"></div>
      <table class="clean"><thead><tr><th>Status</th><th>Symbol</th><th>Side</th><th>Setup 類型</th><th>Open Time</th><th>Open Price</th><th>P&L</th></tr></thead><tbody>${tRows}</tbody></table>
    </div>`;
  }).join('');

  document.getElementById('notesPagination').innerHTML = `
    <button class="btn" onclick="if(notesPage>1){notesPage--;renderDailyNotes();}">◀ Prev</button>
    <span style="font-size:12.5px;color:var(--muted);">Page ${notesPage} of ${totalPages}</span>
    <button class="btn" onclick="if(notesPage<${totalPages}){notesPage++;renderDailyNotes();}">Next ▶</button>`;
}

function switchPage(pageId, btn) {
  activePage = pageId;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById(`page-${pageId}`).classList.add('active');
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  const titles = { dashboard:'Dashboard', calendar:'Calendar', statistics:'Statistics', equity:'Equity Curve', trades:'Trades', notes:'Daily Notes' };
  document.getElementById('pageHeaderTitle').innerText = titles[pageId];
  refreshActiveView();
}

function refreshActiveView() {
  const s = computeCoreStats(filtered);
  if (activePage === 'dashboard') renderDashboard(s);
  else if (activePage === 'calendar') renderCalendar();
  else if (activePage === 'statistics') renderStatistics(s);
  else if (activePage === 'equity') loadSimDefaults(false);
  else if (activePage === 'trades') renderTradesPage();
  else if (activePage === 'notes') renderDailyNotes();
}

function applyQuickPeriod() {
  const v = document.getElementById('quickPeriod').value;
  if (v === 'ALL') {
    document.getElementById('startDate').value = '';
    document.getElementById('endDate').value = '';
  } else if (v === 'LATEST' && ALL_TRADES.length) {
    const last = ALL_TRADES[ALL_TRADES.length - 1].date;
    document.getElementById('startDate').value = last;
    document.getElementById('endDate').value = last;
  }
  applyFilters();
}

function applyFilters() {
  const sd = document.getElementById('startDate').value;
  const ed = document.getElementById('endDate').value;
  const side = document.getElementById('sideFilter').value;
  const disc = document.getElementById('discFilter').value;
  const sym = document.getElementById('symFilter').value.trim().toUpperCase();

  filtered = ALL_TRADES.filter(t => {
    if (sd && t.date < sd) return false;
    if (ed && t.date > ed) return false;
    if (side !== 'ALL' && t.side !== side) return false;
    if (disc === 'OK' && !t.disciplined) return false;
    if (disc === 'VIOL' && t.disciplined) return false;
    if (sym && !t.symbol.includes(sym)) return false;
    return true;
  });
  tradesPage = 1; notesPage = 1;
  refreshActiveView();
}

function resetFilters() {
  document.getElementById('quickPeriod').value = 'ALL';
  document.getElementById('startDate').value = '';
  document.getElementById('endDate').value = '';
  document.getElementById('sideFilter').value = 'ALL';
  document.getElementById('discFilter').value = 'ALL';
  document.getElementById('symFilter').value = '';
  sortCol = null; sortAsc = true; singleNoteDate = '';
  applyFilters();
}

function initApp() {
  const years = [...new Set(ALL_TRADES.map(t => Number(t.year)))].sort();
  const mNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  const ms = document.getElementById('calMonthSel'), ys = document.getElementById('calYearSel');
  mNames.forEach((nm, i) => { const o = document.createElement('option'); o.value = i+1; o.innerText = nm; ms.appendChild(o); });
  (years.length ? years : [2026]).forEach(y => { const o = document.createElement('option'); o.value = y; o.innerText = y; ys.appendChild(o); });

  const btn = document.getElementById('btnDisplayMode');
  if (btn) {
      btn.innerText = (localStorage.getItem('app_display_mode') || 'usd') === 'usd' ? '切換為 R 倍數' : '切換為 $ 金額';
      btn.className = (localStorage.getItem('app_display_mode') || 'usd') === 'usd' ? 'btn btn-primary' : 'btn btn-green';
  }
  if (ALL_TRADES.length) {
    const first = ALL_TRADES[0].date, last = ALL_TRADES[ALL_TRADES.length - 1].date;
    calYear = Number(last.slice(0,4)); calMonth = Number(last.slice(5,7));
    ms.value = calMonth; ys.value = calYear;
    document.getElementById('acctDateBox').innerHTML = `Account Start Date: <b>${first}</b><br>Account End Date: <b>${last}</b>`;
  }
}
initApp();
</script>
</body>
</html>
"""

def generate_html_dashboard(trades):
    # 完美讀取盤前資料庫，連同 1R 設定一起打包送進網頁前端！
    payload = json.dumps({
        "start_equity": DEFAULT_START_EQUITY,
        "chal_start": CHALLENGE_START_DATE,
        "trades": trades,
        "premarket_db": load_premarket_db()
    }, ensure_ascii=False)
    html = HTML_TEMPLATE.replace("__PAYLOAD_JSON__", payload)
    out_html = "trading_dashboard_open.html"
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[完成] 已生成專業開源版儀表板 {out_html}！")
    webbrowser.open("file://" + os.path.realpath(out_html))

def main():
    all_csv_paths = sorted(glob.glob("**/*.csv", recursive=True))
    user_csvs = [f for f in all_csv_paths if os.path.basename(f).lower() not in IGNORE_CSVS]
    if not user_csvs:
        print("[提示] 找不到券商匯出的 CSV 檔案。")
        return

    cash_fills, fallback_fills = [], []
    for path in user_csvs:
        c_fills = parse_tos_cash_balance(path)
        if c_fills:
            cash_fills.extend(c_fills)
        else:
            fallback_fills.extend(parse_tos_trade_history_fallback(path))

    final_fills = cash_fills if cash_fills else fallback_fills
    if not final_fills: return
    trades = build_round_trip_trades(final_fills)
    generate_html_dashboard(trades)

if __name__ == "__main__":
    main()
