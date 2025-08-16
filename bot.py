# bot.py — v 1.0.1

import os, math, time, json, threading, traceback
from datetime import datetime
from flask import Flask, request, jsonify, Response, render_template_string, abort
from dotenv import load_dotenv
import requests
from binance.client import Client
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC,
    FUTURE_ORDER_TYPE_MARKET, FUTURE_ORDER_TYPE_LIMIT
)
from binance.exceptions import BinanceAPIException, BinanceRequestException
from websocket import WebSocketApp

# ========= ENV =========
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

WEBHOOK_CHECK_ENABLED = os.getenv("WEBHOOK_CHECK_ENABLED", "false").lower() == "true"
WEBHOOK_DEFAULT_VALID = int(os.getenv("WEBHOOK_DEFAULT_VALID", "60"))

USE_WEBSOCKET = os.getenv("USE_WEBSOCKET", "true").lower() == "true"
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "2"))
TOP_N = int(os.getenv("TOP_N", "30"))
VOLUME_REFRESH_SEC = int(os.getenv("VOLUME_REFRESH_SEC", "10"))
MIN_QUOTE_VOLUME_USDT = float(os.getenv("MIN_QUOTE_VOLUME_USDT", "0"))

SYMBOL_WHITELIST = [s.strip().upper() for s in os.getenv("SYMBOL_WHITELIST", "").split(",") if s.strip()]
SYMBOL_BLACKLIST = [s.strip().upper() for s in os.getenv("SYMBOL_BLACKLIST", "").split(",") if s.strip()]

# 임계값은 BPS(=만분율)로 받음. 예) 50 bps = 0.50%
ENTRY_THRESHOLD_BPS = float(os.getenv("ENTRY_THRESHOLD_BPS", "50"))
EXIT_THRESHOLD_BPS  = float(os.getenv("EXIT_THRESHOLD_BPS", "10"))
STOP_LOSS_BPS       = float(os.getenv("STOP_LOSS_BPS", "30"))
ALLOW_NEGATIVE_BASIS = os.getenv("ALLOW_NEGATIVE_BASIS", "true").lower() == "true"

# 새로 추가된 자동 청산 관련 설정
AUTO_CLOSE_PNL_PCT = float(os.getenv("AUTO_CLOSE_PNL_PCT", "0.3")) # PNL % 기반 청산
MAX_HOLDING_MIN = int(os.getenv("MAX_HOLDING_MIN", "120")) # 포지션 최대 유지 시간(분)

LEVERAGE = int(os.getenv("LEVERAGE", "3"))
MAX_SPOT_USDT = float(os.getenv("MAX_SPOT_USDT", "10"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "3"))

# 수수료/슬리피지 (상대값, 예: 0.001 = 10bps)
TAKER_FEE_BPS_SPOT = float(os.getenv("TAKER_FEE_BPS_SPOT", "10")) / 10000.0
TAKER_FEE_BPS_FUT  = float(os.getenv("TAKER_FEE_BPS_FUT", "2")) / 10000.0
SLIPPAGE_BPS       = float(os.getenv("SLIPPAGE_BPS", "1")) / 10000.0

REBALANCE_ENABLED = os.getenv("REBALANCE_ENABLED", "false").lower() == "true"
REBALANCE_TARGET_SPOT_RATIO = float(os.getenv("REBALANCE_TARGET_SPOT_RATIO", "0.5"))
REBALANCE_BAND = float(os.getenv("REBALANCE_BAND", "0.1"))
REBALANCE_INTERVAL_SEC = int(os.getenv("REBALANCE_INTERVAL_SEC", "120"))

USE_NATIVE_OCO_SPOT = os.getenv("USE_NATIVE_OCO_SPOT", "false").lower() == "true"
NATIVE_OCO_TP_PCT = float(os.getenv("NATIVE_OCO_TP_PCT", "0.003"))
NATIVE_OCO_SL_PCT = float(os.getenv("NATIVE_OCO_SL_PCT", "0.003"))

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))
DASHBOARD_REFRESH_MS = int(os.getenv("DASHBOARD_REFRESH_MS", "5000"))

# 선택적 보호들
API_TOKEN = os.getenv("API_TOKEN", "").strip()  # 프로그램성 API 보호
DASHBOARD_AUTH_TOKEN = os.getenv("DASHBOARD_AUTH_TOKEN", "").strip()  # 대시보드/관련 API 보호

# 텔레그램
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
THREAD_ID_DEFAULT = os.getenv("TELEGRAM_THREAD_ID_DEFAULT", "").strip()
THREAD_MAP_JSON = os.getenv("TELEGRAM_THREAD_MAP_JSON", "").strip()
try:
    THREAD_MAP = json.loads(THREAD_MAP_JSON) if THREAD_MAP_JSON else {}
except Exception:
    THREAD_MAP = {}

# 표시 포맷
DEC_QTY  = int(os.getenv("DEC_QTY", "6"))
DEC_PCT  = int(os.getenv("DEC_PCT", "3"))
DEC_USDT = int(os.getenv("DEC_USDT", "4"))
DEC_VOL  = int(os.getenv("DEC_VOL", "0"))

EMOJI_LIVE, EMOJI_TESTNET, EMOJI_DRY = "🟢", "🧪", "📝"
EMOJI_OPEN, EMOJI_CLOSE, EMOJI_EXEC, EMOJI_ERROR = "🚀","🧹","⚙️","❗"
LABEL_LIVE, LABEL_TEST, LABEL_DRY = "LIVE","TESTNET","DRY"
LABEL_OPEN, LABEL_CLOSE, LABEL_EXEC, LABEL_FAIL = "OPEN","CLOSE","EXEC","FAIL"

# ========= Binance Client =========
client = Client(API_KEY, API_SECRET, testnet=USE_TESTNET)

# ========= App / State =========
app = Flask(__name__)

state_lock = threading.Lock()
ws_lock = threading.Lock()

# 필터 캐시
symbol_filters_cache = {}    # spot symbol -> filters
filters_cache_ts = {}
fut_symbol_filters_cache = {}  # futures filters map
fut_filters_cache_ts = {}
FILTERS_TTL = 300  # 5m

# 포지션/시세/Tx
positions = {}       # {symbol: {...}}
transactions = []    # list of closes
spot_last, fut_last = {}, {}
top_universe = []    # [{symbol, volume}]
last_top_symbols = [] # 대시보드 top5
logs_ring, MAX_LOGS = [], 400
RUNTIME_BLACKLIST = set()

# 펀딩(목록 캐시)
funding_list_cache, funding_list_ts = {}, {}
FUNDING_TTL = 180  # 3m
# 레버리지 캐시
last_lev = {}

# ========= 유틸 =========
def now_ms(): return int(time.time()*1000)

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with state_lock:
        logs_ring.append(line)
        if len(logs_ring) > MAX_LOGS:
            del logs_ring[:len(logs_ring)-MAX_LOGS]

def fmt_qty(x):  return f"{x:.{DEC_QTY}f}"
def fmt_usdt(x): return f"{x:.{DEC_USDT}f}"

def run_tag():
    if DRY_RUN: return f"{EMOJI_DRY} [{LABEL_DRY}]"
    if USE_TESTNET: return f"{EMOJI_TESTNET} [{LABEL_TEST}]"
    return f"{EMOJI_LIVE} [{LABEL_LIVE}]"

def tg_thread_id(symbol=None):
    try:
        if symbol and symbol in THREAD_MAP: return int(THREAD_MAP[symbol])
        if THREAD_ID_DEFAULT: return int(THREAD_ID_DEFAULT)
    except Exception:
        pass
    return None

def _telegram_post(text, thread_id=None, max_retry=5):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID): return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if thread_id: data["message_thread_id"] = thread_id
    delay = 1
    for _ in range(max_retry):
        try:
            r = requests.post(url, data=data, timeout=8)
            if r.status_code == 200: return
            log(f"[TG ERR] {r.status_code} {r.text[:200]}")
        except Exception as e:
            log(f"[TG EXC] {e}")
        time.sleep(delay); delay = min(delay*2, 16)

def telegram_send(text, symbol=None):
    _telegram_post(text, thread_id=tg_thread_id(symbol))

def backoff_call(fn, *args, max_wait=60, **kwargs):
    delay = 1
    while True:
        try:
            return fn(*args, **kwargs)
        except (BinanceAPIException, BinanceRequestException, requests.exceptions.RequestException) as e:
            log(f"[REST BACKOFF] {fn.__name__}: {e}")
            time.sleep(delay)
            delay = min(delay*2, max_wait)

# ========= 심볼 필터 =========
def apply_symbol_filters(rows, debug=False):
    merged_bl = set(SYMBOL_BLACKLIST) | set(RUNTIME_BLACKLIST)
    out = []
    for r in rows:
        sym = r.get("symbol","")
        if not sym.endswith("USDT"):
            if debug: log(f"[FILTER] drop {sym}: not USDT")
            continue
        qv = float(r.get("quoteVolume", 0.0))
        if MIN_QUOTE_VOLUME_USDT and qv < MIN_QUOTE_VOLUME_USDT:
            if debug: log(f"[FILTER] drop {sym}: qv {qv} < MIN_QV {MIN_QUOTE_VOLUME_USDT}")
            continue
        if SYMBOL_WHITELIST and sym not in SYMBOL_WHITELIST:
            if debug: log(f"[FILTER] drop {sym}: not in WL")
            continue
        if sym in merged_bl:
            if debug: log(f"[FILTER] drop {sym}: in BL/runtimeBL")
            continue
        out.append((sym, qv))
    out.sort(key=lambda x: x[1], reverse=True)
    return [{"symbol": s, "volume": v} for s, v in out]

def get_top_symbols_rest(limit=TOP_N):
    fut_rows = backoff_call(client.futures_ticker)
    fut_rows.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
    top_fut = fut_rows[:limit]
    spot_list = backoff_call(client.get_all_tickers)
    spot_syms = {t["symbol"] for t in spot_list}
    combined = [r for r in top_fut if r.get("symbol","") in spot_syms]
    syms = apply_symbol_filters(combined, debug=False)
    if not syms:
        log(f"[ENTRY] no symbols after filters (MIN_QV={MIN_QUOTE_VOLUME_USDT}, WL/BL/runtimeBL)")
    return syms

# ========= 가격/베이시스 =========
def futures_price(symbol):
    try: return float(backoff_call(client.futures_symbol_ticker, symbol=symbol)["price"])
    except Exception: return None

def spot_price(symbol):
    try: return float(backoff_call(client.get_symbol_ticker, symbol=symbol)["price"])
    except Exception: return None

def calc_basis_pct_live(symbol):
    with ws_lock:
        s, f = spot_last.get(symbol), fut_last.get(symbol)
    if s is None: s = spot_price(symbol)
    if f is None: f = futures_price(symbol)
    if s is None or f is None or s <= 0: return None
    return (f - s) / s

# ========= LOT SIZE (현물/선물) =========
def round_step(qty, step):
    if step <= 0: return qty
    prec = max(0, int(round(-math.log(step, 10), 0)))
    return float(f"{math.floor(qty/step)*step:.{prec}f}")

def lot_adjust(symbol, qty):
    ts_now = time.time()
    with state_lock:
        filters = symbol_filters_cache.get(symbol)
        if filters and ts_now - filters_cache_ts.get(symbol, 0) < FILTERS_TTL:
            pass
        else:
            info = backoff_call(client.get_symbol_info, symbol=symbol)
            filters = {f["filterType"]: f for f in info["filters"]}
            symbol_filters_cache[symbol] = filters
            filters_cache_ts[symbol] = ts_now
    lot = filters.get("LOT_SIZE", {})
    step = float(lot.get("stepSize","0.000001"))
    minq = float(lot.get("minQty","0"))
    maxq = float(lot.get("maxQty","1e12"))
    q = round_step(qty, step)
    return max(min(q, maxq), minq)

def fut_lot_adjust(symbol, qty):
    ts = time.time()
    with state_lock:
        filters = fut_symbol_filters_cache.get(symbol)
        fresh = filters and ts - fut_filters_cache_ts.get(symbol, 0) < FILTERS_TTL
    if not fresh:
        info = backoff_call(client.futures_exchange_info)
        fmap = {s["symbol"]: {f["filterType"]: f for f in s["filters"]} for s in info["symbols"]}
        filters = fmap.get(symbol, {})
        with state_lock:
            fut_symbol_filters_cache[symbol] = filters
            fut_filters_cache_ts[symbol] = ts
    lot = filters.get("LOT_SIZE", {})
    step = float(lot.get("stepSize","0.001"))
    minq = float(lot.get("minQty","0"))
    maxq = float(lot.get("maxQty","1e12"))
    q = round_step(qty, step)
    return max(min(q, maxq), minq)

def change_leverage(symbol, lev):
    try:
        if last_lev.get(symbol) == lev: return
        backoff_call(client.futures_change_leverage, symbol=symbol, leverage=lev)
        last_lev[symbol] = lev
    except Exception as e:
        log(f"[WARN] leverage set fail {symbol}: {e}")

# ========= 주문 =========
def tg_order_message(market, symbol, side, qty, order_type, price=None, reduce_only=False, resp=None, action="EXEC"):
    tag = run_tag()
    em = {"OPEN": EMOJI_OPEN, "CLOSE": EMOJI_CLOSE, "EXEC": EMOJI_EXEC}.get(action, EMOJI_EXEC)
    px = "" if (price is None or (isinstance(price,(int,float)) and price==0)) else f" @ {price}"
    ro = " reduceOnly" if reduce_only else ""
    rid = None
    try: rid = resp.get("orderId") or resp.get("orderListId") or resp.get("clientOrderId")
    except Exception: pass
    rid_txt = f" id={rid}" if rid else ""
    mock = " (mock)" if resp and resp.get("mock") else ""
    return f"{tag} {em} [{action}] {market} {symbol} {side} qty={fmt_qty(qty)} {order_type}{px}{ro}{rid_txt}{mock}"

def tg_error_message(symbol, when, exc):
    tag = run_tag()
    code = exc.code if isinstance(exc, BinanceAPIException) else None
    msg = exc.message if isinstance(exc, BinanceAPIException) else str(exc)
    body = f"{tag} {EMOJI_ERROR} [{LABEL_FAIL}] {symbol} at {when}"
    if code is not None: body += f" | code={code}"
    if msg: body += f" | {msg[:200]}"
    return body

def place_spot_order(symbol, side, qty, order_type=ORDER_TYPE_MARKET, price=None):
    if DRY_RUN:
        resp = {"mock": True}
        log(f"[DRY][SPOT] {symbol} {side} qty={fmt_qty(qty)} {order_type} price={price}")
        telegram_send(tg_order_message("SPOT", symbol, side, qty, order_type, price=price, resp=resp), symbol=symbol)
        return resp
    try:
        if order_type == ORDER_TYPE_MARKET:
            resp = backoff_call(client.create_order, symbol=symbol, side=side, type=ORDER_TYPE_MARKET, quantity=qty)
        elif order_type == ORDER_TYPE_LIMIT:
            if price is None: raise ValueError("limit price required")
            resp = backoff_call(client.create_order, symbol=symbol, side=side, type=ORDER_TYPE_LIMIT,
                                 timeInForce=TIME_IN_FORCE_GTC, quantity=qty, price=f"{price}")
        else:
            raise ValueError("unsupported spot order type")
        telegram_send(tg_order_message("SPOT", symbol, side, qty, order_type, price=price, resp=resp), symbol=symbol)
        return resp
    except Exception as e:
        telegram_send(tg_error_message(symbol, "place_spot_order", e), symbol=symbol)
        raise

def place_spot_oco_sell(symbol, qty, price, stop_price, stop_limit_price):
    if DRY_RUN:
        resp = {"mock": True}
        log(f"[DRY][SPOT-OCO] SELL {symbol} qty={fmt_qty(qty)} price={price}, stop={stop_price}/{stop_limit_price}")
        desc = f"tp:{price} sp:{stop_price}/{stop_limit_price}"
        telegram_send(tg_order_message("SPOT-OCO", symbol, "SELL", qty, "OCO", price=desc, resp=resp), symbol=symbol)
        return resp
    try:
        resp = backoff_call(
            client.create_oco_order,
            symbol=symbol, side=SIDE_SELL, quantity=qty,
            price=f"{price}", stopPrice=f"{stop_price}",
            stopLimitPrice=f"{stop_limit_price}", stopLimitTimeInForce=TIME_IN_FORCE_GTC
        )
        desc = f"tp:{price} sp:{stop_price}/{stop_limit_price}"
        telegram_send(tg_order_message("SPOT-OCO", symbol, "SELL", qty, "OCO", price=desc, resp=resp), symbol=symbol)
        return resp
    except Exception as e:
        telegram_send(tg_error_message(symbol, "spot_oco", e), symbol=symbol)
        log(f"[WARN] spot OCO failed {symbol}: {e}")
        return None

def place_fut_order(symbol, side, qty, order_type=FUTURE_ORDER_TYPE_MARKET, price=None, reduce_only=False):
    if DRY_RUN:
        resp = {"mock": True}
        log(f"[DRY][FUT] {symbol} {side} qty={fmt_qty(qty)} {order_type} reduce={reduce_only} price={price}")
        telegram_send(tg_order_message("FUT", symbol, side, qty, order_type, price=price, reduce_only=reduce_only, resp=resp), symbol=symbol)
        return resp
    change_leverage(symbol, LEVERAGE)
    try:
        params = dict(symbol=symbol, side=side, type=order_type, quantity=qty)
        if reduce_only: params["reduceOnly"] = True
        if order_type == FUTURE_ORDER_TYPE_LIMIT:
            if price is None: raise ValueError("limit price required")
            params["price"] = f"{price}"
            params["timeInForce"] = TIME_IN_FORCE_GTC
        resp = backoff_call(client.futures_create_order, **params)
        telegram_send(tg_order_message("FUT", symbol, side, qty, order_type, price=price, reduce_only=reduce_only, resp=resp), symbol=symbol)
        return resp
    except Exception as e:
        telegram_send(tg_error_message(symbol, "place_fut_order", e), symbol=symbol)
        raise

def fut_close_market(symbol, qty, side):
    return place_fut_order(symbol, side, qty, FUTURE_ORDER_TYPE_MARKET, reduce_only=True)

# ========= 펀딩(목록 캐시) =========
def funding_sum_since(symbol, t_open_ms):
    try:
        nowt = time.time()
        need_refresh = (symbol not in funding_list_cache) or (nowt - funding_list_ts.get(symbol, 0) > FUNDING_TTL)
        if need_refresh:
            end = now_ms()
            start = end - 48*3600*1000
            rates = backoff_call(client.futures_funding_rate, symbol=symbol, startTime=start, endTime=end)
            funding_list_cache[symbol] = [(int(r["fundingTime"]), float(r["fundingRate"])) for r in rates]
            funding_list_ts[symbol] = nowt
        return sum(rate for ts, rate in funding_list_cache[symbol] if ts >= t_open_ms)
    except Exception as e:
        log(f"[FUNDING WARN] {symbol}: {e}")
        return 0.0

# ========= 포지션 =========
def enter_position(symbol, mode):
    with ws_lock:
        s_cached = spot_last.get(symbol)
        f_cached = fut_last.get(symbol)
    s = s_cached if s_cached else spot_price(symbol)
    if not s or s <= 0:
        log(f"[OPEN SKIP] {symbol}: invalid spot price")
        return False

    # 수량: 현물 목표수량 → 각 시장 LOT 규칙에 맞춤 → 최소값으로 완전헤지
    spot_qty_target = MAX_SPOT_USDT / s
    spot_qty = lot_adjust(symbol, spot_qty_target)
    fpx = f_cached if f_cached else futures_price(symbol)
    if fpx is None: fpx = s  # 보수적 fallback
    fut_qty = fut_lot_adjust(symbol, spot_qty)
    qty = min(spot_qty, fut_qty)
    if qty <= 0:
        log(f"[OPEN SKIP] {symbol}: qty<=0 after lot adjust (spot={spot_qty}, fut={fut_qty})")
        return False

    # === 새로 추가: 베이시스 기반 청산 목표치 계산 ===
    b_in = calc_basis_pct_live(symbol) or 0.0
    tp_basis_pct = EXIT_THRESHOLD_BPS / 10000.0
    sl_basis_pct = STOP_LOSS_BPS / 10000.0

    if mode == "carry":
        tp_basis = b_in - tp_basis_pct
        sl_basis = b_in + sl_basis_pct
    else: # reverse
        tp_basis = b_in + tp_basis_pct
        sl_basis = b_in - sl_basis_pct
    # =================================================

    try:
        if mode == "carry":
            place_spot_order(symbol, SIDE_BUY, qty, ORDER_TYPE_MARKET)
            place_fut_order(symbol, SIDE_SELL, qty, FUTURE_ORDER_TYPE_MARKET)
        else:
            place_spot_order(symbol, SIDE_SELL, qty, ORDER_TYPE_MARKET)
            place_fut_order(symbol, SIDE_BUY,  qty, FUTURE_ORDER_TYPE_MARKET)

        f_now = f_cached if f_cached else futures_price(symbol)

        with state_lock:
            positions[symbol] = {
                "dir": "carry_pos" if mode=="carry" else "reverse_pos",
                "qty": qty,
                "s_in": s,
                "f_in": f_now,
                "basis_in": b_in,
                "tp_basis": tp_basis, # 추가
                "sl_basis": sl_basis, # 추가
                "t_open": now_ms()
            }
        telegram_send(f"{run_tag()} {EMOJI_OPEN} [{LABEL_OPEN}] {symbol} {mode} qty={fmt_qty(qty)} basis_in={(b_in*100):.{DEC_PCT}f}%", symbol=symbol)
        if USE_NATIVE_OCO_SPOT and mode=="carry":
            tp_price = s * (1 + NATIVE_OCO_TP_PCT)
            sl_price = s * (1 - NATIVE_OCO_SL_PCT)
            sl_limit = sl_price * 0.999
            place_spot_oco_sell(symbol, qty, tp_price, sl_price, sl_limit)
        return True
    except Exception as e:
        telegram_send(tg_error_message(symbol, "enter_position", e), symbol=symbol)
        log(f"[OPEN FAIL] {symbol}: {e}")
        return False

def pos_symbol(pos):
    for k,v in positions.items():
        if v is pos: return k
    return "UNKNOWN"

def estimate_unrealized_pnl(pos, s_now, f_now):
    s_in = pos["s_in"]
    f_in = pos.get("f_in") or s_in
    qty  = pos["qty"]
    t_open = pos.get("t_open", now_ms())

    if pos["dir"] == "carry_pos":
        pnl_price = ((f_in - f_now) - (s_now - s_in)) / s_in
    else:
        pnl_price = ((s_in - s_now) - (f_in - f_now)) / s_in

    fees_total = (TAKER_FEE_BPS_SPOT + TAKER_FEE_BPS_FUT) * 2
    slip_total = SLIPPAGE_BPS * 2
    fund = funding_sum_since(pos_symbol(pos), t_open)
    funding_adj = fund if pos["dir"] == "carry_pos" else -fund

    pnl_total = pnl_price + funding_adj - fees_total - slip_total
    notional = qty * s_in
    return {
        "pnl_price_rel": pnl_price,
        "funding_rel": funding_adj,
        "fees_rel": -fees_total,
        "slip_rel": -slip_total,
        "pnl_total_rel": pnl_total,
        "pnl_total_usdt": pnl_total * notional,
        "notional": notional
    }

def calc_realized_pnl(pos, s_now, f_now):
    s_in = pos["s_in"]; f_in = pos.get("f_in") or s_in; qty = pos["qty"]
    funding_rate_sum = funding_sum_since(pos_symbol(pos), pos["t_open"])
    funding_usdt = funding_rate_sum * (qty * f_in)
    if pos["dir"] == "reverse_pos": funding_usdt *= -1
    fees_usdt_spot = (qty*s_in*TAKER_FEE_BPS_SPOT) + (qty*s_now*TAKER_FEE_BPS_SPOT)
    fees_usdt_fut  = (qty*f_in*TAKER_FEE_BPS_FUT)  + (qty*f_now*TAKER_FEE_BPS_FUT)
    fees_usdt = fees_usdt_spot + fees_usdt_fut
    if pos["dir"] == "carry_pos":
        pnl_price_usdt = (s_now - s_in)*qty + (f_in - f_now)*qty
    else:
        pnl_price_usdt = (s_in - s_now)*qty + (f_now - f_in)*qty
    pnl_total_usdt = pnl_price_usdt + funding_usdt - fees_usdt
    notional_usdt = qty * s_in
    pnl_total_rel = (pnl_total_usdt / notional_usdt) if notional_usdt>0 else 0
    return {"pnl_usdt": pnl_total_usdt, "pnl_pct": pnl_total_rel, "fees_usdt": fees_usdt,
            "funding_usdt": funding_usdt, "pnl_price_usdt": pnl_price_usdt, "notional_usdt": notional_usdt}

def close_position(symbol, reason="manual"):
    with state_lock: pos = positions.get(symbol)
    if not pos: return False
    qty = pos["qty"]
    try:
        if pos["dir"] == "carry_pos":
            place_spot_order(symbol, SIDE_SELL, qty, ORDER_TYPE_MARKET)
            fut_close_market(symbol, qty, SIDE_BUY)
        else:
            place_spot_order(symbol, SIDE_BUY, qty, ORDER_TYPE_MARKET)
            fut_close_market(symbol, qty, SIDE_SELL)
        t_close = now_ms()
        s_now, f_now = spot_price(symbol), futures_price(symbol)
        if s_now is not None and f_now is not None:
            realized = calc_realized_pnl(pos, s_now, f_now)
            tx = {"symbol": symbol, "t_open": pos["t_open"], "t_close": t_close,
                  "holding_time_ms": t_close - pos["t_open"],
                  "notional_usdt": realized["notional_usdt"],
                  "pnl_usdt": realized["pnl_usdt"], "pnl_pct": realized["pnl_pct"],
                  "fees_usdt": realized["fees_usdt"], "reason": reason}
            with state_lock: transactions.append(tx)
        with state_lock: positions.pop(symbol, None)
        telegram_send(f"{run_tag()} {EMOJI_CLOSE} [{LABEL_CLOSE}] {symbol} reason={reason}", symbol=symbol)
        return True
    except Exception as e:
        telegram_send(tg_error_message(symbol, "close_position", e), symbol=symbol)
        log(f"[CLOSE FAIL] {symbol}: {e}")
        return False

# ========= 자동 청산 루프 (새로 추가된 기능) =========
def check_positions_loop():
    while True:
        with state_lock:
            active_positions = list(positions.items())
        
        for symbol, pos in active_positions:
            try:
                # 1. 가격 정보 업데이트
                s_now = spot_last.get(symbol)
                f_now = fut_last.get(symbol)
                
                # WS 가격 정보가 없으면 REST API로 가져오기 (폴백)
                if s_now is None: s_now = spot_price(symbol)
                if f_now is None: f_now = futures_price(symbol)
                
                if s_now is None or f_now is None:
                    continue # 가격 정보가 없으면 다음 포지션으로 이동

                # 2. PNL 및 시간 계산
                pnl_data = estimate_unrealized_pnl(pos, s_now, f_now)
                pnl_pct = pnl_data["pnl_total_rel"] * 100 # 백분율로 변환
                holding_time_min = (now_ms() - pos["t_open"]) / 60000

                # 3. 청산 조건 확인
                reason = None
                
                # A. 베이시스 기반 청산 (이익 실현 및 손절매)
                current_basis_pct = calc_basis_pct_live(symbol)
                is_carry = (pos["dir"] == "carry_pos")
                
                # 이익 실현 (베이시스가 목표치에 도달하거나 초과)
                if is_carry and current_basis_pct <= pos["tp_basis"]:
                    reason = "take_profit_basis"
                elif not is_carry and current_basis_pct >= pos["tp_basis"]:
                    reason = "take_profit_basis"
                
                # 손절매 (베이시스가 목표치에 도달하거나 초과)
                if not reason:
                    if is_carry and current_basis_pct >= pos["sl_basis"]:
                        reason = "stop_loss_basis"
                    elif not is_carry and current_basis_pct <= pos["sl_basis"]:
                        reason = "stop_loss_basis"
                
                # B. PNL 기반 청산 (절대 이익/손실)
                if not reason:
                    if abs(pnl_pct) >= AUTO_CLOSE_PNL_PCT:
                        reason = "pnl_target_hit"
                        
                # C. 시간 기반 청산 (장기 포지션 청산)
                if not reason:
                    if holding_time_min >= MAX_HOLDING_MIN:
                        reason = "max_holding_time"

                # 4. 포지션 청산
                if reason:
                    log(f"[AUTO CLOSE] {symbol} | Reason: {reason} | PNL: {pnl_pct:.2f}% | Holding: {holding_time_min:.1f} min")
                    close_position(symbol, reason=reason)

            except Exception as e:
                log(f"[ERR check_positions_loop] {symbol}: {e}\n{traceback.format_exc()}")
        
        time.sleep(SCAN_INTERVAL_SEC)


# ========= WS =========
def ws_run_forever(url, on_message, name):
    backoff = 1
    while True:
        try:
            ws = WebSocketApp(url, on_message=on_message,
                              on_error=lambda w,e: log(f"[WS {name} err] {e}"),
                              on_close=lambda *a: log(f"[WS {name} closed]"))
            log(f"[WS {name}] connecting...")
            ws.run_forever(ping_interval=20, ping_timeout=10)
            log(f"[WS {name}] disconnected")
        except Exception as e:
            log(f"[WS {name} exception] {e}")
        time.sleep(backoff); backoff = min(backoff*2, 60); log(f"[WS {name}] reconnect in {backoff}s")

def _ws_spot():
    url = "wss://stream.binance.com/ws/!miniTicker@arr"
    def on_msg(ws, msg):
        try:
            arr = json.loads(msg)
            with ws_lock:
                for x in arr:
                    s = x.get("s"); c = x.get("c")
                    if s and s.endswith("USDT") and c: spot_last[s] = float(c)
        except Exception: pass
    ws_run_forever(url, on_msg, "spot")

def _ws_fut():
    url = "wss://fstream.binance.com/ws/!markPrice@arr"
    def on_msg(ws, msg):
        try:
            arr = json.loads(msg)
            with ws_lock:
                for x in arr:
                    s = x.get("s"); p = x.get("p") or x.get("P")
                    if s and s.endswith("USDT") and p: fut_last[s] = float(p)
        except Exception: pass
    ws_run_forever(url, on_msg, "futures")

def start_websockets():
    threading.Thread(target=_ws_spot, daemon=True).start()
    threading.Thread(target=_ws_fut, daemon=True).start()
    threading.Thread(target=refresh_top_universe_loop, daemon=True).start()

# ========= 자동 선정 =========
def refresh_top_universe_loop():
    global top_universe
    while True:
        try:
            syms = get_top_symbols_rest(limit=TOP_N)
            with ws_lock:
                top_universe = syms
        except Exception as e:
            log(f"[ERR refresh_top_universe] {e}")
        time.sleep(VOLUME_REFRESH_SEC)

# ========= 엔트리 루프 =========
def entry_loop():
    entry_th = ENTRY_THRESHOLD_BPS/10000.0  # bps → 비율
    while True:
        try:
            with state_lock:
                open_cnt = len(positions)
            if open_cnt >= MAX_CONCURRENT_POSITIONS:
                time.sleep(SCAN_INTERVAL_SEC); continue

            # WS 후보 비면 REST 폴백
            if USE_WEBSOCKET:
                with ws_lock: syms_with_vol = list(top_universe)
                if not syms_with_vol:
                    log("[ENTRY] top_universe empty; WS not ready — fallback to REST")
                    syms_with_vol = get_top_symbols_rest(limit=TOP_N)
            else:
                syms_with_vol = get_top_symbols_rest(limit=TOP_N)

            if not syms_with_vol:
                time.sleep(SCAN_INTERVAL_SEC); continue

            view, best = [], None
            for item in syms_with_vol:
                sym, vol = item["symbol"], item["volume"]
                with state_lock:
                    if sym in positions: continue
                b = calc_basis_pct_live(sym)
                if b is None: continue
                view.append({"symbol": sym, "basis_pct": b, "volume": vol})

            # 스코어 최대값 선정
            for it in view:
                sym, b = it["symbol"], it["basis_pct"]
                if b >= 0:
                    score, mode = b, "carry"
                else:
                    if not ALLOW_NEGATIVE_BASIS: continue
                    score, mode = -b, "reverse"
                if (best is None) or (score > best[0]): best = (score, sym, mode, b)

            # 대시보드용 Top5
            top_view = []
            for it in view:
                bb = it["basis_pct"]
                top_view.append({"symbol": it["symbol"], "basis_pct": bb, "expected": abs(bb),
                                 "mode": "carry" if bb>=0 else "reverse", "volume": it.get("volume",0)})
            with state_lock:
                global last_top_symbols
                last_top_symbols = sorted(top_view, key=lambda x: x["expected"], reverse=True)[:5]

            if not best:
                time.sleep(SCAN_INTERVAL_SEC); continue

            score, sym, mode, b_now = best
            if score < entry_th:    # 임계치 미달
                time.sleep(SCAN_INTERVAL_SEC); continue

            ok = enter_position(sym, mode)
            if ok:
                with state_lock: oc = len(positions)
                log(f"[SELECTED ENTRY] {sym} mode={mode} expected={(score*100):.{DEC_PCT}f}% basis_now={(b_now*100):.{DEC_PCT}f}% open_cnt={oc}")
        except Exception as e:
            log(f"[ERR entry_loop] {e}\n{traceback.format_exc()}")
        time.sleep(SCAN_INTERVAL_SEC)

# ========= 리밸런싱(옵션) =========
def get_spot_usdt():
    try:
        b = backoff_call(client.get_asset_balance, asset="USDT")
        return float(b["free"]) + float(b["locked"])
    except Exception: return 0.0

def get_futures_usdt():
    try:
        bal = backoff_call(client.futures_account_balance)
        for x in bal:
            if x.get("asset") == "USDT":
                return float(x.get("balance", 0))
        return 0.0
    except Exception: return 0.0

def rebalance_transfer(asset, amount, type_):
    if DRY_RUN:
        log(f"[DRY][REBAL] transfer {asset} {amount} type={type_}")
        telegram_send(f"{run_tag()} {EMOJI_EXEC} [REBAL] transfer {float(amount):.{DEC_USDT}f} {asset}")
        return
    try:
        backoff_call(client.futures_account_transfer, asset=asset, amount=f"{amount}", type=type_)
        telegram_send(f"{run_tag()} {EMOJI_EXEC} [REBAL] transfer {float(amount):.{DEC_USDT}f} {asset}")
    except Exception as e:
        telegram_send(tg_error_message("USDT", "rebalance_transfer", e))

def rebalance_loop():
    while True:
        try:
            if not REBALANCE_ENABLED:
                time.sleep(REBALANCE_INTERVAL_SEC); continue
            spot_u = get_spot_usdt()
            fut_u  = get_futures_usdt()
            total = spot_u + fut_u
            if total <= 0:
                time.sleep(REBALANCE_INTERVAL_SEC); continue
            spot_ratio = spot_u / total
            low  = REBALANCE_TARGET_SPOT_RATIO - REBALANCE_BAND
            high = REBALANCE_TARGET_SPOT_RATIO + REBALANCE_BAND
            if spot_ratio < low:
                delta = (REBALANCE_TARGET_SPOT_RATIO - spot_ratio) * total
                amt = max(0.0, min(delta, fut_u))
                if amt > 0: rebalance_transfer("USDT", amt, 2)
            elif spot_ratio > high:
                delta = (spot_ratio - REBALANCE_TARGET_SPOT_RATIO) * total
                amt = max(0.0, min(delta, spot_u))
                if amt > 0: rebalance_transfer("USDT", amt, 1)
        except Exception as e:
            log(f"[ERR rebalance] {e}")
        time.sleep(REBALANCE_INTERVAL_SEC)

# ========= 보호 로직 =========
def require_token(req):
    """프로그램성 API 보호용 헤더 토큰 (X-API-TOKEN). 대시보드 토큰과 별개."""
    if not API_TOKEN: return True
    return req.headers.get("X-API-TOKEN") == API_TOKEN

def dashboard_token_ok(req):
    """대시보드 및 연동 API 보호 토큰. 쿼리파라미터 token 또는 헤더 X-DASHBOARD-TOKEN 허용."""
    if not DASHBOARD_AUTH_TOKEN:
        return True
    header_token = req.headers.get("X-DASHBOARD-TOKEN", "")
    query_token = req.args.get("token", "")
    return (header_token == DASHBOARD_AUTH_TOKEN) or (query_token == DASHBOARD_AUTH_TOKEN)

def require_dashboard_token():
    if not dashboard_token_ok(request):
        abort(401)

def check_webhook_valid(data: dict) -> bool:
    if not WEBHOOK_CHECK_ENABLED: return True
    ts = data.get("timestamp")
    valid_for = int(data.get("valid_for", WEBHOOK_DEFAULT_VALID))
    if not ts: return False
    return (now_ms() - int(ts)) <= valid_for * 1000

# ========= API =========
@app.get("/health")
def health(): return jsonify({"status":"UP","ts":now_ms()})

@app.post("/webhook")
def webhook():
    # 프로그램성 webhook은 기존 API_TOKEN 정책 유지
    if not require_token(request): return jsonify({"status":"unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    if not check_webhook_valid(data): return jsonify({"status":"expired"}), 400
    sym = (data.get("symbol") or "").upper()
    action = (data.get("action") or "").lower()
    if not sym: return jsonify({"status":"bad_request"}), 400
    with state_lock:
        if sym in positions: return jsonify({"status":"position_open"}), 409
        if len(positions) >= MAX_CONCURRENT_POSITIONS: return jsonify({"status":"too_many_positions"}), 429
    mode = "carry" if action=="buy" else "reverse"
    ok = enter_position(sym, mode)
    return jsonify({"status":"ok" if ok else "fail"})

@app.post("/enter")
def enter_api():
    # 대시보드에서 호출되는 API → 대시보드 토큰 요구
    require_dashboard_token()
    data = request.get_json(silent=True) or {}
    sym = (data.get("symbol") or "").upper()
    mode = (data.get("mode") or "").lower()
    if not sym or mode not in ("carry","reverse"): return jsonify({"status":"bad_request"}), 400
    with state_lock:
        if sym in positions: return jsonify({"status":"position_open"}), 409
        if len(positions) >= MAX_CONCURRENT_POSITIONS: return jsonify({"status":"too_many_positions"}), 429
    ok = enter_position(sym, mode)
    return jsonify({"status":"ok" if ok else "fail"})

@app.post("/toggle_blacklist")
def toggle_blacklist():
    require_dashboard_token()
    data = request.get_json(silent=True) or {}
    sym = (data.get("symbol") or "").upper()
    if not sym: return jsonify({"status":"bad_request"}), 400
    with state_lock:
        if sym in RUNTIME_BLACKLIST:
            RUNTIME_BLACKLIST.remove(sym); status = "removed"
        else:
            RUNTIME_BLACKLIST.add(sym); status = "added"
    return jsonify({"status":"ok","action":status,"symbol":sym})

@app.post("/close")
def close_api():
    require_dashboard_token()
    data = request.get_json(silent=True) or {}
    sym = (data.get("symbol") or "").upper()
    if not sym: return jsonify({"status":"bad_request"}), 400
    ok = close_position(sym, reason="manual")
    return jsonify({"status":"ok" if ok else "fail"})

@app.get("/status")
def status_api():
    require_dashboard_token()
    with state_lock:
        p_count = len(positions)
        logs_view = logs_ring[:]
        tx_view = transactions[:]
        top_view = last_top_symbols[:]
        bl_view = list(RUNTIME_BLACKLIST)
        pos_view = []
        for sym, p in positions.items():
            s_now, f_now = spot_last.get(sym), fut_last.get(sym)
            pnl_data = estimate_unrealized_pnl(p, s_now, f_now) if s_now and f_now else None
            pos_view.append({
                "symbol": sym, "dir": p["dir"], "qty": p["qty"], "s_in": p["s_in"],
                "f_in": p["f_in"], "basis_in": p["basis_in"],
                "t_open": p["t_open"], "pnl": pnl_data,
                "s_now": s_now, "f_now": f_now
            })
    return jsonify({
        "status": "ok",
        "positions": pos_view,
        "transactions": tx_view,
        "logs": logs_view,
        "top_symbols": top_view,
        "blacklist": bl_view,
        "open_count": p_count
    })

@app.get("/")
def dashboard():
    require_dashboard_token()
    # PNL 관련 표시 포맷
    def fmt_pnl_usdt(x):
        return f"+{x:.{DEC_USDT}f}" if x>0 else f"{x:.{DEC_USDT}f}"
    
    # 숫자 포맷
    def fmt_num(x, dec):
        if x is None: return "N/A"
        if x >= 1000: return f"{x:,.{dec}f}"
        return f"{x:.{dec}f}"
    
    # HTML 템플릿
    html_template = """
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Arbitrage Bot Dashboard</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 0; padding: 0; background-color: #1a1a2e; color: #fff; }
            .container { padding: 20px; max-width: 1200px; margin: auto; }
            h1, h2 { color: #94b8e2; border-bottom: 2px solid #2e2e50; padding-bottom: 5px; }
            .status-box { background-color: #2e2e50; padding: 15px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 4px 8px rgba(0,0,0,0.2); }
            .status-box p { margin: 5px 0; }
            .grid-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
            .card { background-color: #2e2e50; padding: 15px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.2); transition: transform 0.2s; }
            .card:hover { transform: translateY(-5px); }
            .table-container { overflow-x: auto; }
            table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #3a3a5e; }
            th { background-color: #4a4a7a; color: #e0e0ff; }
            tr:hover { background-color: #3a3a5e; }
            .logs-container { max-height: 400px; overflow-y: scroll; background-color: #16162a; border-radius: 8px; padding: 15px; }
            .log-line { margin: 0; padding: 2px 0; font-family: monospace; font-size: 12px; }
            .btn { background-color: #0f4c75; color: white; border: none; padding: 8px 12px; border-radius: 5px; cursor: pointer; margin-left: 5px; }
            .btn-close { background-color: #e74c3c; }
            .btn-blacklist { background-color: #f39c12; }
            .pnl-pos { color: #2ecc71; }
            .pnl-neg { color: #e74c3c; }
            .header-status { font-weight: bold; }
        </style>
    </head>
    <body>
    <div class="container">
        <h1>Arbitrage Bot Dashboard {{ run_tag() }}</h1>
        <div class="status-box">
            <p><strong>Status:</strong> <span class="header-status">{{ EMOJI_LIVE if not DRY_RUN else EMOJI_DRY }} Running</span></p>
            <p><strong>Open Positions:</strong> <span id="open-count">0</span> / {{ MAX_CONCURRENT_POSITIONS }}</p>
            <p><strong>Last Updated:</strong> <span id="last-updated">N/A</span></p>
        </div>

        <div class="grid-container">
            <div class="card">
                <h2>Open Positions</h2>
                <div class="table-container">
                    <table id="positions-table">
                        <thead>
                            <tr>
                                <th>Symbol</th>
                                <th>Dir</th>
                                <th>Qty</th>
                                <th>Entry Price</th>
                                <th>Basis In</th>
                                <th>Current Basis</th>
                                <th>Unrealized PNL</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="card">
                <h2>Live Top Symbols ({{TOP_N}} Universe)</h2>
                <div class="table-container">
                    <table id="top-symbols-table">
                        <thead>
                            <tr>
                                <th>Symbol</th>
                                <th>Basis</th>
                                <th>Expected</th>
                                <th>Mode</th>
                                <th>Volume</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="card">
                <h2>Recent Transactions</h2>
                <div class="table-container">
                    <table id="transactions-table">
                        <thead>
                            <tr>
                                <th>Symbol</th>
                                <th>PNL (USDT)</th>
                                <th>PNL (%)</th>
                                <th>Holding Time</th>
                                <th>Reason</th>
                            </tr>
                        </thead>
                        <tbody>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="card">
                <h2>Runtime Blacklist</h2>
                <ul id="blacklist-list">
                </ul>
            </div>
        </div>

        <div class="card" style="margin-top: 20px;">
            <h2>Logs</h2>
            <div id="logs-container" class="logs-container">
            </div>
        </div>
    </div>

    <script>
        const DASHBOARD_AUTH_TOKEN = "{{ DASHBOARD_AUTH_TOKEN }}";
        const REFRESH_MS = {{ DASHBOARD_REFRESH_MS }};

        function fetchData() {
            let url = "/status";
            if (DASHBOARD_AUTH_TOKEN) {
                url += `?token=${DASHBOARD_AUTH_TOKEN}`;
            }

            fetch(url)
                .then(response => response.json())
                .then(data => {
                    updateDashboard(data);
                })
                .catch(error => console.error('Error fetching data:', error));
        }

        function updateDashboard(data) {
            document.getElementById('open-count').innerText = data.open_count;
            document.getElementById('last-updated').innerText = new Date().toLocaleTimeString();

            // Open Positions
            const posTableBody = document.getElementById('positions-table').querySelector('tbody');
            posTableBody.innerHTML = '';
            data.positions.forEach(pos => {
                const row = posTableBody.insertRow();
                row.insertCell(0).innerText = pos.symbol;
                row.insertCell(1).innerText = pos.dir;
                row.insertCell(2).innerText = pos.qty.toFixed({{ DEC_QTY }});
                row.insertCell(3).innerText = pos.s_in.toFixed({{ DEC_USDT }});
                row.insertCell(4).innerText = (pos.basis_in * 100).toFixed({{ DEC_PCT }}) + '%';
                
                const currentBasis = (pos.pnl && pos.s_now && pos.s_now > 0) ? ((pos.f_now - pos.s_now) / pos.s_now * 100).toFixed({{ DEC_PCT }}) + '%' : 'N/A';
                row.insertCell(5).innerText = currentBasis;
                
                const pnlCell = row.insertCell(6);
                if (pos.pnl) {
                    const pnlText = `USDT: ${pos.pnl.pnl_total_usdt.toFixed({{ DEC_USDT }})} / %: ${(pos.pnl.pnl_total_rel * 100).toFixed({{ DEC_PCT }})}`;
                    pnlCell.innerText = pnlText;
                    pnlCell.className = pos.pnl.pnl_total_usdt >= 0 ? 'pnl-pos' : 'pnl-neg';
                } else {
                    pnlCell.innerText = 'N/A';
                }
                
                const actionCell = row.insertCell(7);
                const closeBtn = document.createElement('button');
                closeBtn.innerText = 'Close';
                closeBtn.className = 'btn btn-close';
                closeBtn.onclick = () => closePosition(pos.symbol);
                actionCell.appendChild(closeBtn);
            });

            // Top Symbols
            const topTableBody = document.getElementById('top-symbols-table').querySelector('tbody');
            topTableBody.innerHTML = '';
            data.top_symbols.forEach(sym => {
                const row = topTableBody.insertRow();
                row.insertCell(0).innerText = sym.symbol;
                row.insertCell(1).innerText = (sym.basis_pct * 100).toFixed({{ DEC_PCT }}) + '%';
                row.insertCell(2).innerText = (sym.expected * 100).toFixed({{ DEC_PCT }}) + '%';
                row.insertCell(3).innerText = sym.mode;
                row.insertCell(4).innerText = sym.volume.toLocaleString();
                const actionCell = row.insertCell(5);
                
                const enterBtn = document.createElement('button');
                enterBtn.innerText = 'Enter';
                enterBtn.className = 'btn';
                enterBtn.onclick = () => enterPosition(sym.symbol, sym.mode);
                actionCell.appendChild(enterBtn);
                
                const blacklistBtn = document.createElement('button');
                blacklistBtn.innerText = 'BL';
                blacklistBtn.className = 'btn btn-blacklist';
                blacklistBtn.onclick = () => toggleBlacklist(sym.symbol);
                actionCell.appendChild(blacklistBtn);
            });
            
            // Recent Transactions
            const txTableBody = document.getElementById('transactions-table').querySelector('tbody');
            txTableBody.innerHTML = '';
            data.transactions.slice().reverse().forEach(tx => {
                const row = txTableBody.insertRow();
                row.insertCell(0).innerText = tx.symbol;
                const pnlCell = row.insertCell(1);
                pnlCell.innerText = tx.pnl_usdt.toFixed({{ DEC_USDT }});
                pnlCell.className = tx.pnl_usdt >= 0 ? 'pnl-pos' : 'pnl-neg';
                row.insertCell(2).innerText = (tx.pnl_pct * 100).toFixed({{ DEC_PCT }}) + '%';
                
                const holdingTimeMin = tx.holding_time_ms / 60000;
                row.insertCell(3).innerText = holdingTimeMin.toFixed(1) + ' min';
                row.insertCell(4).innerText = tx.reason;
            });
            
            // Blacklist
            const blList = document.getElementById('blacklist-list');
            blList.innerHTML = '';
            data.blacklist.forEach(sym => {
                const li = document.createElement('li');
                li.innerText = sym;
                const btn = document.createElement('button');
                btn.innerText = 'Remove';
                btn.className = 'btn btn-blacklist';
                btn.onclick = () => toggleBlacklist(sym);
                li.appendChild(btn);
                blList.appendChild(li);
            });

            // Logs
            const logsContainer = document.getElementById('logs-container');
            logsContainer.innerHTML = '';
            data.logs.forEach(logLine => {
                const p = document.createElement('p');
                p.className = 'log-line';
                p.innerText = logLine;
                logsContainer.appendChild(p);
            });
            logsContainer.scrollTop = logsContainer.scrollHeight;
        }

        function enterPosition(symbol, mode) {
            fetch("/enter", {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-DASHBOARD-TOKEN": DASHBOARD_AUTH_TOKEN },
                body: JSON.stringify({ symbol: symbol, mode: mode })
            })
            .then(response => response.json())
            .then(data => {
                if(data.status !== "ok") alert("Failed to open position: " + data.status);
            });
        }

        function closePosition(symbol) {
            fetch("/close", {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-DASHBOARD-TOKEN": DASHBOARD_AUTH_TOKEN },
                body: JSON.stringify({ symbol: symbol })
            })
            .then(response => response.json())
            .then(data => {
                if(data.status !== "ok") alert("Failed to close position: " + data.status);
            });
        }

        function toggleBlacklist(symbol) {
            fetch("/toggle_blacklist", {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-DASHBOARD-TOKEN": DASHBOARD_AUTH_TOKEN },
                body: JSON.stringify({ symbol: symbol })
            })
            .then(response => response.json())
            .then(data => {
                console.log(data);
            });
        }
        
        setInterval(fetchData, REFRESH_MS);
        fetchData(); // Initial load
    </script>
    </body>
    </html>
    """
    return render_template_string(html_template, **globals())

if __name__ == "__main__":
    if USE_WEBSOCKET:
        start_websockets()
    
    # === 추가된 자동 청산 루프 시작 ===
    threading.Thread(target=check_positions_loop, daemon=True).start()
    
    threading.Thread(target=entry_loop, daemon=True).start()
    threading.Thread(target=rebalance_loop, daemon=True).start()
    
    log(f"[{run_tag()}] Starting server on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True)