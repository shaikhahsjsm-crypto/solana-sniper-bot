import os, re, json, time, sqlite3, threading
from datetime import datetime, timezone, timedelta
from typing import Any
import requests
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(APP_DIR, "sniper.db"))
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "CART3R-Solana-Paper-Sniper/1.0"}
HTTP = requests.Session()
HTTP.headers.update(HEADERS)
DB_LOCK = threading.Lock()

DEFAULTS = {
    "starting_balance": 100.0,
    "trade_size": 5.0,
    "max_positions": 2,
    "max_trades_day": 5,
    "max_token_age_min": 30,
    "min_liquidity": 15000.0,
    "min_volume_m5": 5000.0,
    "stop_loss_pct": 20.0,
    "take_profit_pct": 40.0,
    "daily_loss_limit": 10.0,
    "max_rugcheck_score": 35,
    "max_slippage_pct": 2.0,
}

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with DB_LOCK:
        con = db()
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tokens (
          mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, pool TEXT, dex TEXT,
          created_at TEXT, liquidity REAL, volume_m5 REAL, price_usd REAL,
          risk_score REAL, risk_json TEXT, last_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS trades (
          id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, symbol TEXT,
          side TEXT, amount_usd REAL, token_qty REAL, price_usd REAL,
          fee_usd REAL, pnl_usd REAL DEFAULT 0, status TEXT,
          opened_at TEXT, closed_at TEXT, reason TEXT
        );
        """)
        for k, v in DEFAULTS.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, json.dumps(v)))
        con.commit()
        con.close()

def get_settings():
    con = db()
    rows = con.execute("SELECT key,value FROM settings").fetchall()
    con.close()
    vals = {r["key"]: json.loads(r["value"]) for r in rows}
    return {**DEFAULTS, **vals}

def save_settings(vals):
    with DB_LOCK:
        con = db()
        for k, v in vals.items():
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, json.dumps(v)))
        con.commit()
        con.close()

def api_get(url, params=None, timeout=12):
    response = HTTP.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()

def token_mint_from_pool(pool: dict) -> str:
    attrs = pool.get("attributes") or {}
    direct = attrs.get("base_token_address")
    if direct:
        return str(direct)
    rel = (pool.get("relationships") or {}).get("base_token") or {}
    data = rel.get("data") or {}
    ident = str(data.get("id") or "")
    return ident.split("_", 1)[1] if ident.startswith("solana_") else ident

def parse_created(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def fetch_new_pools():
    data = api_get(f"{GECKO}/networks/solana/new_pools", params={"page": 1, "include": "base_token,quote_token,dex"})
    pools = data.get("data") or []
    included = {x.get("id"): x for x in data.get("included", [])}
    out = []
    for p in pools:
        a = p.get("attributes") or {}
        mint = token_mint_from_pool(p)
        if not mint or len(mint) < 30:
            continue
        created = a.get("pool_created_at")
        base = (p.get("relationships") or {}).get("base_token", {}).get("data", {}).get("id", "")
        token_info = included.get(base, {}).get("attributes", {})
        volume = a.get("volume_usd") or {}
        txns = a.get("transactions") or {}
        out.append({
            "mint": mint,
            "symbol": token_info.get("symbol") or a.get("name", "UNKNOWN").split("/")[0].strip(),
            "name": token_info.get("name") or a.get("name", "Unknown"),
            "pool": a.get("address") or p.get("id", "").split("_")[-1],
            "dex": (p.get("relationships") or {}).get("dex", {}).get("data", {}).get("id", "unknown"),
            "created_at": created,
            "liquidity": float(a.get("reserve_in_usd") or 0),
            "volume_m5": float(volume.get("m5") or 0),
            "price_usd": float(a.get("base_token_price_usd") or 0),
            "buys_m5": int((txns.get("m5") or {}).get("buys") or 0),
            "sells_m5": int((txns.get("m5") or {}).get("sells") or 0),
        })
    return out

def rugcheck(mint: str):
    # Fail closed: no entry if the independent risk endpoint is unavailable.
    data = api_get(f"{RUGCHECK}/tokens/{mint}/report/summary", timeout=15)
    score = float(data.get("score_normalised") or data.get("score") or 0)
    risks = data.get("risks") or []
    authority_flags = []
    for risk in risks:
        name = str(risk.get("name", "")).lower()
        level = str(risk.get("level", "")).lower()
        if any(x in name for x in ("mint authority", "freeze authority", "mutable", "honeypot")):
            authority_flags.append({"name": risk.get("name"), "level": level, "description": risk.get("description")})
    return {"score": score, "risks": risks, "lp_locked_pct": data.get("lpLockedPct"), "authority_flags": authority_flags}

def assess_token(t, cfg):
    reasons = []
    created = parse_created(t.get("created_at"))
    if created is None:
        reasons.append("Missing/invalid pool creation time")
    else:
        age = (datetime.now(timezone.utc) - created).total_seconds() / 60
        if age < 0 or age > cfg["max_token_age_min"]:
            reasons.append(f"Pool age outside limit ({age:.1f} min)")
    if t["liquidity"] < cfg["min_liquidity"]:
        reasons.append("Liquidity below minimum")
    if t["volume_m5"] < cfg["min_volume_m5"]:
        reasons.append("5-minute volume below minimum")
    if t["price_usd"] <= 0:
        reasons.append("No valid price")
    if t["sells_m5"] > t["buys_m5"] * 2 + 3:
        reasons.append("Heavy sell imbalance")
    try:
        risk = rugcheck(t["mint"])
        t["risk"] = risk
        if risk["score"] > cfg["max_rugcheck_score"]:
            reasons.append(f"RugCheck score too high ({risk['score']})")
        if risk["authority_flags"]:
            reasons.append("Token authority/permission warning")
    except Exception as e:
        t["risk"] = None
        reasons.append(f"Risk check unavailable: {type(e).__name__}")
    return len(reasons) == 0, reasons

def upsert_token(t):
    risk = t.get("risk") or {}
    with DB_LOCK:
        con = db()
        con.execute("""INSERT OR REPLACE INTO tokens
        (mint,symbol,name,pool,dex,created_at,liquidity,volume_m5,price_usd,risk_score,risk_json,last_seen)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t["mint"],t["symbol"],t["name"],t["pool"],t["dex"],t.get("created_at"),
         t["liquidity"],t["volume_m5"],t["price_usd"],risk.get("score"),
         json.dumps(risk),now_iso()))
        con.commit()
        con.close()

def get_open_positions():
    con = db()
    rows = con.execute("SELECT * FROM trades WHERE side='BUY' AND status='OPEN' ORDER BY id DESC").fetchall()
    con.close()
    return [dict(r) for r in rows]

def trades_today():
    start = datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
    con = db()
    rows = con.execute("SELECT * FROM trades WHERE opened_at >= ? ORDER BY id DESC", (start,)).fetchall()
    con.close()
    return [dict(r) for r in rows]

def current_cash(cfg):
    con = db()
    buys = con.execute("SELECT COALESCE(SUM(amount_usd+fee_usd),0) v FROM trades WHERE side='BUY' AND status='OPEN'").fetchone()["v"]
    realized = con.execute("SELECT COALESCE(SUM(pnl_usd),0) v FROM trades WHERE side='SELL'").fetchone()["v"]
    con.close()
    return cfg["starting_balance"] - float(buys) + float(realized)

def realized_today():
    rows = trades_today()
    return sum(float(x["pnl_usd"] or 0) for x in rows if x["side"] == "SELL")

def simulate_buy(t, cfg):
    positions = get_open_positions()
    today = trades_today()
    if len(positions) >= cfg["max_positions"]:
        return "Max open positions reached"
    if len([x for x in today if x["side"] == "BUY"]) >= cfg["max_trades_day"]:
        return "Daily trade limit reached"
    if realized_today() <= -cfg["daily_loss_limit"]:
        return "Daily loss limit reached"
    size = min(cfg["trade_size"], current_cash(cfg))
    if size <= 0:
        return "Insufficient virtual cash"
    # Conservative simulated entry: include configured slippage and fee.
    slip = cfg["max_slippage_pct"] / 100
    fee_rate = 0.01
    entry = t["price_usd"] * (1 + slip)
    fee = size * fee_rate
    qty = (size - fee) / entry
    with DB_LOCK:
        con = db()
        con.execute("""INSERT INTO trades(mint,symbol,side,amount_usd,token_qty,price_usd,fee_usd,status,opened_at,reason)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (t["mint"],t["symbol"],"BUY",size,qty,entry,fee,"OPEN",now_iso(),"Paper entry; simulated slippage and 1% fee"))
        con.commit()
        con.close()
    return f"Paper BUY opened: ${size:.2f}"

def close_position(pos, price, reason, cfg):
    slip = cfg["max_slippage_pct"] / 100
    exit_price = max(0, price * (1 - slip))
    gross = pos["token_qty"] * exit_price
    fee = gross * 0.01
    pnl = gross - fee - pos["amount_usd"] - pos["fee_usd"]
    with DB_LOCK:
        con = db()
        con.execute("UPDATE trades SET status='CLOSED',closed_at=?,pnl_usd=?,reason=? WHERE id=?",
                    (now_iso(),pnl,reason,pos["id"]))
        con.execute("""INSERT INTO trades(mint,symbol,side,amount_usd,token_qty,price_usd,fee_usd,pnl_usd,status,opened_at,closed_at,reason)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pos["mint"],pos["symbol"],"SELL",gross,pos["token_qty"],exit_price,fee,pnl,"CLOSED",
                     now_iso(),now_iso(),reason))
        con.commit()
        con.close()
    return pnl

def update_positions(cfg):
    positions = get_open_positions()
    if not positions:
        return []
    # One multi-pool request keeps polling within the public API's modest rate limit.
    pools = list(dict.fromkeys([p["mint"] for p in positions]))
    results = []
    for pos in positions:
        try:
            con = db()
            tok = con.execute("SELECT pool FROM tokens WHERE mint=?", (pos["mint"],)).fetchone()
            con.close()
            if not tok or not tok["pool"]:
                continue
            pdata = api_get(f"{GECKO}/networks/solana/pools/{tok['pool']}")
            attrs = (pdata.get("data") or {}).get("attributes") or {}
            price = float(attrs.get("base_token_price_usd") or 0)
            if price <= 0:
                continue
            pnl_pct = ((price - pos["price_usd"]) / pos["price_usd"]) * 100
            if pnl_pct <= -cfg["stop_loss_pct"]:
                pnl = close_position(pos, price, "Stop-loss triggered", cfg)
                results.append(f"{pos['symbol']}: stop-loss, P&L ${pnl:.2f}")
            elif pnl_pct >= cfg["take_profit_pct"]:
                pnl = close_position(pos, price, "Take-profit triggered", cfg)
                results.append(f"{pos['symbol']}: take-profit, P&L ${pnl:.2f}")
        except Exception as e:
            results.append(f"{pos['symbol']}: price refresh failed ({type(e).__name__})")
        time.sleep(6.1)
    return results

def scan_once(cfg):
    pools = fetch_new_pools()
    accepted, rejected, actions = [], [], []
    known = set()
    con = db()
    known = {r["mint"] for r in con.execute("SELECT mint FROM tokens").fetchall()}
    con.close()
    for t in pools:
        if t["mint"] in known:
            continue
        ok, reasons = assess_token(t, cfg)
        upsert_token(t)
        if ok:
            accepted.append(t)
            action = simulate_buy(t, cfg)
            actions.append(f'{t["symbol"]}: {action}')
        else:
            rejected.append({"symbol":t["symbol"],"mint":t["mint"],"reasons":reasons})
        # RugCheck is a third-party API; throttle requests.
        time.sleep(6.1)
    return {"pools_seen":len(pools),"accepted":accepted,"rejected":rejected,"actions":actions}

def load_frames():
    con = db()
    tokens = pd.read_sql_query("SELECT symbol,mint,dex,created_at,liquidity,volume_m5,price_usd,risk_score,last_seen FROM tokens ORDER BY last_seen DESC LIMIT 100", con)
    trades = pd.read_sql_query("SELECT id,symbol,side,amount_usd,price_usd,fee_usd,pnl_usd,status,opened_at,closed_at,reason FROM trades ORDER BY id DESC LIMIT 200", con)
    con.close()
    return tokens, trades

st.set_page_config(page_title="CART3R Solana Sniper", page_icon="🔴", layout="wide")
init_db()
cfg = get_settings()

st.markdown("""
<style>
.stApp {background:#070707;color:#f4eeee}
[data-testid="stSidebar"] {background:#100909;border-right:1px solid #6f2020}
h1,h2,h3 {color:#ff4b4b}
div[data-testid="stMetric"] {background:#150b0b;border:1px solid #642020;padding:14px;border-radius:12px}
.stButton>button {border:1px solid #a52b2b;background:#260d0d;color:#fff;border-radius:9px}
.stButton>button:hover {border-color:#ff4b4b;color:#ff7777}
div[data-testid="stDataFrame"] {border:1px solid #522020;border-radius:10px}
</style>
""", unsafe_allow_html=True)

st.title("🔴 CART3R.JAILBREAK // SOLANA SNIPER")
st.caption("PAPER TRADING ONLY • New-pool discovery • Fail-closed risk checks • No wallet connection")
st.warning("SIMULATION ONLY — no private keys, wallet signing, or real transactions are used. Market data and risk reports may be delayed or incomplete.")

with st.sidebar:
    st.header("Control panel")
    st.success("PAPER MODE LOCKED")
    auto_refresh = st.toggle("Auto-refresh dashboard (30 sec)", value=False)
    if st.button("🔎 Scan new pools", use_container_width=True):
        with st.spinner("Scanning GeckoTerminal and checking token risk..."):
            try:
                result = scan_once(cfg)
                st.session_state["last_scan"] = result
                st.session_state["scan_time"] = now_iso()
                st.success(f"Scanned {result['pools_seen']} pools; {len(result['accepted'])} passed.")
            except Exception as e:
                st.error(f"Scanner error: {type(e).__name__}: {e}")
    if st.button("↻ Refresh prices / exits", use_container_width=True):
        with st.spinner("Refreshing open positions..."):
            messages = update_positions(cfg)
            st.session_state["exit_messages"] = messages
            st.success("Position refresh complete.")
    if st.button("🚨 Emergency close all (paper)", use_container_width=True):
        st.session_state["confirm_close"] = True
    if st.session_state.get("confirm_close"):
        st.warning("Close all simulated positions at their last recorded entry price? This is an approximation.")
        if st.button("Confirm paper close-all"):
            for pos in get_open_positions():
                close_position(pos, pos["price_usd"], "Emergency paper close", cfg)
            st.session_state["confirm_close"] = False
            st.success("All open paper positions closed.")
    st.divider()
    st.caption("Public data sources: GeckoTerminal + RugCheck")

cash = current_cash(cfg)
open_positions = get_open_positions()
daily_pnl = realized_today()
tokens_df, trades_df = load_frames()
total_realized = float(trades_df.loc[trades_df["side"]=="SELL","pnl_usd"].fillna(0).sum()) if not trades_df.empty else 0.0

m1,m2,m3,m4 = st.columns(4)
m1.metric("Virtual cash", f"${cash:,.2f}")
m2.metric("Open positions", f"{len(open_positions)} / {cfg['max_positions']}")
m3.metric("Realized P&L", f"${total_realized:,.2f}")
m4.metric("Today's realized P&L", f"${daily_pnl:,.2f}")

if "last_scan" in st.session_state:
    r = st.session_state["last_scan"]
    st.info(f"Last scan: {st.session_state.get('scan_time','')} · pools: {r['pools_seen']} · passed: {len(r['accepted'])} · rejected: {len(r['rejected'])}")
    for a in r["actions"]:
        st.write("•", a)
if st.session_state.get("exit_messages"):
    for msg in st.session_state["exit_messages"]:
        st.write("•", msg)

tab1,tab2,tab3,tab4,tab5 = st.tabs(["📊 Overview","🟢 Open positions","🧪 Token scanner","📜 Trade history","⚙️ Risk settings"])
with tab1:
    st.subheader("System overview")
    st.write("The scanner checks new Solana pools, applies age/liquidity/volume filters, then requests a RugCheck summary. Any missing or failed risk report blocks entry.")
    st.write("**Risk rules currently active**")
    st.write(f"- Per-trade size: ${cfg['trade_size']:.2f}")
    st.write(f"- Stop-loss: -{cfg['stop_loss_pct']:.1f}% · Take-profit: +{cfg['take_profit_pct']:.1f}%")
    st.write(f"- Daily loss cap: ${cfg['daily_loss_limit']:.2f} · Daily entries: {cfg['max_trades_day']}")
    st.write(f"- Liquidity minimum: ${cfg['min_liquidity']:,.0f} · 5m volume minimum: ${cfg['min_volume_m5']:,.0f}")
    st.caption("Stop-loss and take-profit are evaluated when you refresh positions. This is not a continuously running execution service.")
    if not trades_df.empty:
        closed = trades_df[(trades_df["side"]=="SELL")]
        if not closed.empty:
            st.subheader("Recent realized P&L")
            chart = closed[["opened_at","pnl_usd"]].copy()
            chart["opened_at"] = pd.to_datetime(chart["opened_at"], errors="coerce")
            st.line_chart(chart.set_index("opened_at")["pnl_usd"].cumsum())
with tab2:
    st.subheader("Open simulated positions")
    if open_positions:
        pos_df = pd.DataFrame(open_positions)
        st.dataframe(pos_df[["id","symbol","mint","amount_usd","token_qty","price_usd","opened_at"]], use_container_width=True, hide_index=True)
    else:
        st.info("No open positions. Run a scan to discover eligible pools.")
with tab3:
    st.subheader("Recently scanned tokens")
    if not tokens_df.empty:
        st.dataframe(tokens_df, use_container_width=True, hide_index=True)
    else:
        st.info("No tokens scanned yet.")
with tab4:
    st.subheader("Trade history")
    if not trades_df.empty:
        st.dataframe(trades_df, use_container_width=True, hide_index=True)
    else:
        st.info("No simulated trades yet.")
with tab5:
    st.subheader("Risk settings")
    st.caption("Settings persist in SQLite. Keep the bot in paper mode while validating the strategy.")
    with st.form("risk_form"):
        c1,c2 = st.columns(2)
        new = {}
        new["starting_balance"] = c1.number_input("Virtual starting balance ($)", min_value=10.0, max_value=100.0, value=float(cfg["starting_balance"]), step=5.0)
        new["trade_size"] = c2.number_input("Trade size ($)", min_value=1.0, max_value=20.0, value=float(cfg["trade_size"]), step=1.0)
        new["max_positions"] = c1.number_input("Maximum open positions", min_value=1, max_value=5, value=int(cfg["max_positions"]))
        new["max_trades_day"] = c2.number_input("Maximum entries per day", min_value=1, max_value=10, value=int(cfg["max_trades_day"]))
        new["max_token_age_min"] = c1.number_input("Maximum pool age (minutes)", min_value=1, max_value=120, value=int(cfg["max_token_age_min"]))
        new["min_liquidity"] = c2.number_input("Minimum liquidity ($)", min_value=1000.0, max_value=1000000.0, value=float(cfg["min_liquidity"]), step=1000.0)
        new["min_volume_m5"] = c1.number_input("Minimum 5-minute volume ($)", min_value=0.0, max_value=1000000.0, value=float(cfg["min_volume_m5"]), step=500.0)
        new["stop_loss_pct"] = c2.number_input("Stop-loss (%)", min_value=1.0, max_value=80.0, value=float(cfg["stop_loss_pct"]))
        new["take_profit_pct"] = c1.number_input("Take-profit (%)", min_value=1.0, max_value=500.0, value=float(cfg["take_profit_pct"]))
        new["daily_loss_limit"] = c2.number_input("Daily loss cap ($)", min_value=1.0, max_value=100.0, value=float(cfg["daily_loss_limit"]))
        new["max_rugcheck_score"] = c1.number_input("Maximum RugCheck score", min_value=0, max_value=10000, value=int(cfg["max_rugcheck_score"]))
        new["max_slippage_pct"] = c2.number_input("Simulated slippage (%)", min_value=0.0, max_value=20.0, value=float(cfg["max_slippage_pct"]))
        submitted = st.form_submit_button("Save risk settings")
        if submitted:
            if new["trade_size"] > new["starting_balance"]:
                st.error("Trade size cannot exceed virtual starting balance.")
            elif new["daily_loss_limit"] > new["starting_balance"]:
                st.error("Daily loss cap cannot exceed virtual starting balance.")
            else:
                save_settings(new)
                st.success("Risk settings saved. Reload the page to apply all values.")

if auto_refresh:
    time.sleep(30)
    st.rerun()
