import os
import time
import logging
import json
from datetime import datetime
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException
from binance.enums import *
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# .env 파일 로드 (일반 환경 변수)
load_dotenv(dotenv_path='.env')
# .env.secrets 파일 로드 (민감한 환경 변수)
load_dotenv(dotenv_path='.env.secrets')

# --- API 키 및 URL 설정 ---
# 테스트넷 현물
BINANCE_TESTNET_API_URL_SPOT = os.getenv('BINANCE_TESTNET_API_URL_SPOT') # https://testnet.binance.vision
API_KEY_SPOT_TESTNET = os.getenv('BINANCE_TESTNET_API_KEY_SPOT')
SECRET_KEY_SPOT_TESTNET = os.getenv('BINANCE_TESTNET_SECRET_KEY_SPOT')

# 테스트넷 선물
BINANCE_TESTNET_API_URL_FUTURES = "https://testnet.binancefuture.com/fapi"
API_KEY_FUTURES_TESTNET = os.getenv('BINANCE_TESTNET_API_KEY_FUTURES')
SECRET_KEY_FUTURES_TESTNET = os.getenv('BINANCE_TESTNET_SECRET_KEY_FUTURES')

# 실제 현물
BINANCE_REAL_API_URL_SPOT = "https://api.binance.com/api"
API_KEY_SPOT_REAL = os.getenv('BINANCE_REAL_API_KEY_SPOT')
SECRET_KEY_SPOT_REAL = os.getenv('BINANCE_REAL_SECRET_KEY_SPOT')

# 실제 선물
BINANCE_REAL_API_URL_FUTURES = "https://fapi.binance.com/fapi"
API_KEY_FUTURES_REAL = os.getenv('BINANCE_REAL_API_KEY_FUTURES')
SECRET_KEY_FUTURES_REAL = os.getenv('BINANCE_REAL_SECRET_KEY_FUTURES')

# 대시보드 인증 토큰 (백엔드에서만 사용)
DASHBOARD_AUTH_TOKEN = os.getenv('DASHBOARD_AUTH_TOKEN')


# --- 필수 환경 변수 확인 (모든 API 키가 존재하는지) ---
if not all([API_KEY_SPOT_TESTNET, SECRET_KEY_SPOT_TESTNET,
            API_KEY_FUTURES_TESTNET, SECRET_KEY_FUTURES_TESTNET,
            API_KEY_SPOT_REAL, SECRET_KEY_SPOT_REAL,
            API_KEY_FUTURES_REAL, SECRET_KEY_FUTURES_REAL,
            DASHBOARD_AUTH_TOKEN]): # DASHBOARD_AUTH_TOKEN도 확인
    logging.error("오류: 모든 현물/선물 테스트넷 및 실제 거래용 API 키와 시크릿 키, 대시보드 인증 토큰이 설정되지 않았습니다.")
    logging.error("`.env.secrets` 파일을 확인하고 모든 변수를 채워주세요.")
    exit(1)

# --- 바이낸스 클라이언트 초기화 ---
# 테스트넷 클라이언트
client_spot_testnet = Client(API_KEY_SPOT_TESTNET, SECRET_KEY_SPOT_TESTNET, tld='com')
client_spot_testnet.API_URL = BINANCE_TESTNET_API_URL_SPOT # '/api' 제거

client_futures_testnet = Client(API_KEY_FUTURES_TESTNET, SECRET_KEY_FUTURES_TESTNET, tld='com')
client_futures_testnet.FUTURES_URL = BINANCE_TESTNET_API_URL_FUTURES

# 실제 거래용 클라이언트
client_spot_real = Client(API_KEY_SPOT_REAL, SECRET_KEY_SPOT_REAL, tld='com')
client_spot_real.API_URL = BINANCE_REAL_API_URL_SPOT

client_futures_real = Client(API_KEY_FUTURES_REAL, SECRET_KEY_FUTURES_REAL, tld='com')
client_futures_real.FUTURES_URL = BINANCE_REAL_API_URL_FUTURES


# 클라이언트 매핑 (환경 및 유형별 접근)
clients = {
    'testnet': {
        'spot': client_spot_testnet,
        'margin': client_spot_testnet, # 마진은 현물 클라이언트 사용
        'futures': client_futures_testnet
    },
    'real': {
        'spot': client_spot_real,
        'margin': client_spot_real, # 마진은 현물 클라이언트 사용
        'futures': client_futures_real
    }
}


app = Flask(__name__)

# --- CORS 설정 ---
CORS(app)

# --- 전역 변수 및 캐시 ---
exchange_info_cache_spot_testnet = {}
exchange_info_cache_futures_testnet = {}
exchange_info_cache_spot_real = {}
exchange_info_cache_futures_real = {}

# API 연결 상태를 각 클라이언트별로 추적
api_status = {
    "testnet_spot": {"status": "미확인", "message": "초기화 중..."},
    "testnet_futures": {"status": "미확인", "message": "초기화 중..."},
    "real_spot": {"status": "미확인", "message": "초기화 중..."},
    "real_futures": {"status": "미확인", "message": "초기화 중..."}
}
trade_history_file = 'trades.json'
trade_history = [] # 봇 가동 기간 동안의 거래 내역 (초기 로드 후 업데이트)


# --- 재시도 설정 ---
binance_api_retry_decorator = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type(BinanceAPIException),
    before_sleep=before_sleep_log(logging.root, logging.INFO),
    reraise=True
)

# --- 헬퍼 함수 ---

def _round_step_size(value, step_size):
    """지정된 step_size에 맞춰 값을 반올림합니다."""
    return float(step_size) * round(float(value) / float(step_size))

@binance_api_retry_decorator
def _get_exchange_info_from_api(client_obj, client_type_str):
    """API에서 거래소 정보를 가져옵니다 (재시도 적용)."""
    logging.info(f"{client_type_str} 거래소 정보 API 호출 시도...")
    if client_type_str.endswith('spot') or client_type_str.endswith('margin'): # 현물/마진 클라이언트
        return client_obj.get_exchange_info()
    elif client_type_str.endswith('futures'): # 선물 클라이언트
        return client_obj.get_exchange_info()
    else:
        raise ValueError("잘못된 클라이언트 타입 문자열입니다.")

def get_symbol_filters(symbol, env_type, client_type):
    """
    특정 심볼의 거래 규칙 (minQty, stepSize, tickSize 등)을 가져오거나 캐시에서 반환합니다.
    """
    cache_map = {
        ('testnet', 'spot'): exchange_info_cache_spot_testnet,
        ('testnet', 'futures'): exchange_info_cache_futures_testnet,
        ('real', 'spot'): exchange_info_cache_spot_real,
        ('real', 'futures'): exchange_info_cache_futures_real,
        ('testnet', 'margin'): exchange_info_cache_spot_testnet, # 마진은 현물 필터 공유
        ('real', 'margin'): exchange_info_cache_spot_real # 마진은 현물 필터 공유
    }
    cache = cache_map.get((env_type, client_type))
    if not cache:
        logging.error(f"잘못된 환경/클라이언트 타입 조합: ({env_type}, {client_type})")
        return None

    if symbol in cache:
        return cache[symbol]

    try:
        client_obj = clients[env_type][client_type]
        info = _get_exchange_info_from_api(client_obj, f"{env_type}_{client_type}")
        for s in info['symbols']:
            if s['symbol'] == symbol:
                filters = {f['filterType']: f for f in s['filters']}
                cache[symbol] = filters
                logging.info(f"{env_type.upper()} {client_type.upper()} {symbol}의 거래 규칙 캐시됨: {filters}")
                return filters
        logging.warning(f"{env_type.upper()} {client_type.upper()} 심볼 {symbol}의 거래 규칙을 찾을 수 없습니다.")
        return None
    except BinanceAPIException as e:
        logging.error(f"{env_type.upper()} {client_type.upper()} 거래 규칙을 가져오는 데 실패했습니다 (API 오류): {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}")
        if e.code in [None, -1022]: # -1022는 SIGNATURE_INVALID 등 시간 동기화 문제일 수 있음
            logging.error("API 키/시크릿, IP 화이트리스트, 시스템 시간 동기화를 확인하세요.")
        return None
    except Exception as e:
        logging.error(f"{env_type.upper()} {client_type.upper()} 거래 규칙을 가져오는 데 알 수 없는 오류 발생: {e}")
        return None

def get_tick_size(symbol, env_type, client_type):
    """특정 심볼의 tickSize를 반환합니다."""
    filters = get_symbol_filters(symbol, env_type, client_type)
    if filters and 'PRICE_FILTER' in filters:
        return float(filters['PRICE_FILTER']['tickSize'])
    return None

def get_lot_size_filters(symbol, env_type, client_type):
    """특정 심볼의 LOT_SIZE 필터를 반환합니다."""
    filters = get_symbol_filters(symbol, env_type, client_type)
    if filters and 'LOT_SIZE' in filters:
        return {
            'minQty': float(filters['LOT_SIZE']['minQty']),
            'maxQty': float(filters['LOT_SIZE']['maxQty']),
            'stepSize': float(filters['LOT_SIZE']['stepSize'])
        }
    return None

def get_min_notional(symbol, env_type, client_type):
    """특정 심볼의 MIN_NOTIONAL 값을 반환합니다."""
    filters = get_symbol_filters(symbol, env_type, client_type)
    if filters and 'MIN_NOTIONAL' in filters:
        return float(filters['MIN_NOTIONAL']['minNotional'])
    return None

def validate_quantity(symbol, quantity, env_type, client_type):
    """
    주문 수량이 심볼의 거래 규칙에 맞는지 검증합니다.
    """
    filters = get_symbol_filters(symbol, env_type, client_type)
    if not filters:
        return False, "심볼의 거래 규칙을 가져올 수 없습니다. API 키/IP 화이트리스트/시간 동기화 확인."

    lot_size_filter = filters.get('LOT_SIZE')
    if lot_size_filter:
        min_qty = float(lot_size_filter.get('minQty'))
        max_qty = float(lot_size_filter.get('maxQty'))
        step_size = float(lot_size_filter.get('stepSize'))

        if quantity < min_qty:
            return False, f"최소 주문 수량 {min_qty}보다 작습니다."
        if quantity > max_qty:
            return False, f"최대 주문 수량 {max_qty}보다 큽니다."

        remainder = (quantity - min_qty) % step_size
        if abs(remainder) > 1e-8 and abs(remainder - step_size) > 1e-8:
            return False, f"주문 수량({quantity})이 스텝 사이즈({step_size})에 맞지 않습니다. (최소 수량 {min_qty} 고려)"
    else:
        logging.warning(f"{env_type.upper()} {client_type.upper()} {symbol}에 대한 LOT_SIZE 필터를 찾을 수 없습니다. 수량 검증을 건너뜕니다.")

    return True, "유효한 수량입니다."


def load_trade_history():
    """trades.json 파일에서 거래 내역을 로드합니다."""
    global trade_history
    if os.path.exists(trade_history_file):
        try:
            with open(trade_history_file, 'r') as f:
                trade_history = json.load(f)
            logging.info(f"{len(trade_history)}개의 거래 내역을 {trade_history_file}에서 로드했습니다.")
        except json.JSONDecodeError as e:
            logging.error(f"거래 내역 파일 로드 실패 (JSON 형식 오류): {e}")
            trade_history = []
        except Exception as e:
            logging.error(f"거래 내역 파일 로드 중 알 수 없는 오류 발생: {e}")
            trade_history = []
    else:
        logging.info("거래 내역 파일이 없습니다. 새롭게 생성합니다.")
        trade_history = []

def save_trade_history():
    """거래 내역을 trades.json 파일에 저장합니다."""
    try:
        with open(trade_history_file, 'w') as f:
            json.dump(trade_history, f, indent=4)
        logging.info("거래 내역을 파일에 저장했습니다.")
    except Exception as e:
        logging.error(f"거래 내역 파일 저장 실패: {e}")

# 시작 시 거래 내역 로드
load_trade_history()

# --- Flask 라우트 정의 ---

@app.route('/')
def redirect_to_dashboard():
    return render_template('dashboard.html')

@app.route('/dashboard.html')
def serve_dashboard():
    return render_template('dashboard.html')

@app.route('/api/login', methods=['POST'])
def login():
    """대시보드 인증 토큰을 검증합니다."""
    data = request.get_json()
    token = data.get('token')
    if token == DASHBOARD_AUTH_TOKEN:
        return jsonify({"success": True, "message": "로그인 성공!"})
    else:
        return jsonify({"success": False, "message": "잘못된 토큰입니다."}), 401 # Unauthorized

@app.route('/api/api_status', methods=['GET'])
def get_api_status():
    """API 연결 상태를 반환합니다."""
    return jsonify(api_status)

@app.route('/api/top_market_data', methods=['GET'])
@binance_api_retry_decorator
def get_top_market_data():
    """
    선택된 환경(trade_env)의 현물 거래소 거래량 상위 50개와 선물 거래소 거래량 상위 50개 중
    서로 겹치는 거래쌍을 선물 거래량 순으로 최대 10개 반환합니다.
    """
    trade_env = request.args.get('trade_env', 'real').strip() # 기본값 실제 거래, 공백 제거
    
    # 선택된 환경의 클라이언트 사용
    spot_client = clients[trade_env]['spot']
    futures_client = clients[trade_env]['futures']

    try:
        # 현물 거래소 상위 50개 심볼 가져오기
        spot_tickers = spot_client.get_ticker()
        spot_top_50 = sorted(spot_tickers, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)[:50]
        spot_symbols_map = {t['symbol']: t for t in spot_top_50}
        logging.info(f"{trade_env.upper()} 현물 상위 50개 심볼 조회 완료: {len(spot_symbols_map)}개.")

        # 선물 거래소 상위 50개 심볼 가져오기
        futures_tickers = futures_client.get_ticker()
        futures_top_50 = sorted(futures_tickers, key=lambda x: float(x.get('volume', 0)), reverse=True)[:50]
        futures_symbols_map = {t['symbol']: t for t in futures_top_50}
        logging.info(f"{trade_env.upper()} 선물 상위 50개 심볼 조회 완료: {len(futures_symbols_map)}개.")

        # 겹치는 심볼 찾기 및 선물 거래량 기준으로 정렬 후 상위 10개만 선택
        common_symbols_sorted = sorted([
            (s, float(futures_symbols_map[s].get('volume', 0)))
            for s in spot_symbols_map.keys() if s in futures_symbols_map
        ], key=lambda x: x[1], reverse=True)[:10] # 상위 10개만 선택
        
        logging.info(f"겹치는 심볼 및 선물 거래량 순 정렬 완료: {len(common_symbols_sorted)}개.")

        result_data = []
        for symbol, _ in common_symbols_sorted:
            spot_price = None
            futures_price = None
            spot_volume = None
            futures_volume = None

            try:
                spot_price = float(spot_symbols_map[symbol]['lastPrice'])
                spot_volume = float(spot_symbols_map[symbol]['quoteVolume'])
            except KeyError:
                logging.warning(f"현물 심볼 {symbol}의 캐시된 시세 정보 부족.")
            except Exception as e:
                logging.warning(f"현물 심볼 {symbol}의 시세 정보 파싱 실패: {e}")

            try:
                futures_price = float(futures_symbols_map[symbol]['lastPrice'])
                futures_volume = float(futures_symbols_map[symbol]['volume'])
            except KeyError:
                logging.warning(f"선물 심볼 {symbol}의 캐시된 시세 정보 부족.")
            except Exception as e:
                logging.warning(f"선물 심볼 {symbol}의 시세 정보 파싱 실패: {e}")

            result_data.append({
                'symbol': symbol,
                'spot_price': spot_price,
                'futures_price': futures_price,
                'spot_volume': spot_volume, 
                'futures_volume': futures_volume
            })
        
        # 시장 데이터 조회 성공 시 해당 환경의 API 상태 업데이트
        api_status[f"{trade_env}_spot"]["status"] = "연결됨"
        api_status[f"{trade_env}_spot"]["message"] = f"{trade_env.upper()} 현물 API 연결 정상."
        api_status[f"{trade_env}_futures"]["status"] = "연결됨"
        api_status[f"{trade_env}_futures"]["message"] = f"{trade_env.upper()} 선물 API 연결 정상."

        return jsonify(result_data)

    except BinanceAPIException as e:
        logging.error(f"{trade_env.upper()} 시장 데이터 조회 실패 (API 오류): {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}")
        # 해당 환경의 API 연결 오류로 간주
        api_status[f"{trade_env}_spot"]["status"] = "오류"
        api_status[f"{trade_env}_spot"]["message"] = f"{trade_env.upper()} 현물 API 오류: {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}"
        api_status[f"{trade_env}_futures"]["status"] = "오류"
        api_status[f"{trade_env}_futures"]["message"] = f"{trade_env.upper()} 선물 API 오류: {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}"
        if e.code in [None, -1022]:
            logging.error("API 키/시크릿, IP 화이트리스트, 시스템 시간 동기화를 확인하세요.")
        raise
    except Exception as e:
        logging.error(f"{trade_env.upper()} 시장 데이터 조회 중 알 수 없는 오류 발생: {e}")
        api_status[f"{trade_env}_spot"]["status"] = "오류"
        api_status[f"{trade_env}_spot"]["message"] = f"{trade_env.upper()} 현물 서버 오류: {str(e)}"
        api_status[f"{trade_env}_futures"]["status"] = "오류"
        api_status[f"{trade_env}_futures"]["message"] = f"{trade_env.upper()} 선물 서버 오류: {str(e)}"
        raise


@app.route('/api/trade', methods=['POST'])
@binance_api_retry_decorator
def trade():
    """
    거래 유형(현물/마진/선물) 및 환경(테스트넷/실제)에 따라 매수 또는 매도 주문을 실행합니다.
    - 매수 시: 거래소 최소 주문 수량 또는 11 USDT 상당 수량 중 더 작은 값, 현재가보다 1 틱 낮은 지정가
    - 매도 시: 현재 보유 코인 잔액 전부, 현재가보다 1 틱 높은 지정가
    - is_close_position: True일 경우, 현재 포지션을 전량 청산
    """
    data = request.get_json()
    symbol = data.get('symbol')
    side = data.get('side') # 'BUY' or 'SELL'
    trade_type = data.get('trade_type', '').strip() # 공백 제거
    trade_env = data.get('trade_env', '').strip()   # 공백 제거
    is_close_position = data.get('is_close_position', False) # 새로운 파라미터
    order_execution_type = data.get('order_execution_type', ORDER_TYPE_LIMIT) # 'LIMIT' or 'MARKET'
    
    if not all([symbol, side, trade_type, trade_env]):
        logging.warning(f"필수 파라미터 누락: symbol={symbol}, side={side}, trade_type={trade_type}, trade_env={trade_env}")
        return jsonify({"error": "심볼, 사이드, 거래 유형, 거래 환경이 필요합니다."}), 400

    client_to_use = None
    if trade_env in clients and trade_type in clients[trade_env]:
        client_to_use = clients[trade_env][trade_type]
    else:
        # 이 오류가 발생하면, 대시보드에서 잘못된 trade_env 또는 trade_type이 전송된 것임
        logging.error(f"잘못된 환경/클라이언트 타입 조합: ({trade_env}, {trade_type})")
        return jsonify({"error": "유효하지 않은 거래 환경 또는 유형입니다. (백엔드 검증)"}), 400

    try:
        # 현재 시장가 및 틱 사이즈 가져오기
        ticker_data = client_to_use.get_ticker(symbol=symbol)
        current_price = float(ticker_data['lastPrice'])
        
        # 호가창에서 best bid/ask 가져오기 (지정가 주문 시 체결 가능성 높은 가격 설정 위함)
        order_book = client_to_use.get_order_book(symbol=symbol, limit=5) # 상위 5개 호가
        best_bid = float(order_book['bids'][0][0]) if order_book['bids'] else current_price
        best_ask = float(order_book['asks'][0][0]) if order_book['asks'] else current_price

        tick_size = get_tick_size(symbol, trade_env, trade_type)
        if not tick_size:
            return jsonify({"error": f"심볼 {symbol}의 틱 사이즈를 가져올 수 없습니다. 거래 규칙 확인 필요."}), 500

        # LOT_SIZE 필터 가져오기
        lot_size_filters = get_lot_size_filters(symbol, trade_env, trade_type)
        if not lot_size_filters:
            return jsonify({"error": f"심볼 {symbol}의 LOT_SIZE 필터를 가져올 수 없습니다. 거래 규칙 확인 필요."}), 500
        min_qty = lot_size_filters['minQty']
        step_size = lot_size_filters['stepSize']

        order_quantity = 0.0
        order_price = 0.0
        order_final_type = order_execution_type # 기본적으로 선택된 주문 유형 사용

        if is_close_position:
            # 포지션 청산 로직 (항상 지정가로 체결 가능성 높은 가격)
            order_final_type = ORDER_TYPE_LIMIT # 청산은 항상 지정가
            current_position_qty = 0.0
            if trade_type == 'spot':
                account_info = client_to_use.get_account()
                for asset in account_info['balances']:
                    if asset['asset'] == symbol.replace('USDT', ''):
                        current_position_qty = float(asset['free'])
                        break
                if current_position_qty <= 0:
                    return jsonify({"message": f"{trade_env.upper()} {trade_type.upper()} {symbol}: 청산할 현물 잔고가 없습니다. (현재 잔고: {current_position_qty})", "order": None}), 200
                order_quantity = _round_step_size(current_position_qty, step_size)
                side = SIDE_SELL # 현물 매도는 항상 SELL
                order_price = _round_step_size(best_bid - tick_size, tick_size) # 매도 시 최고 매수 호가보다 1틱 낮게 (더 공격적)
                if order_price <= 0: order_price = _round_step_size(best_bid, tick_size) # 0이하 방지
                logging.info(f"{trade_env.upper()} {trade_type.upper()} {symbol}: 현물 청산 주문 준비 (매도): 수량 {order_quantity}, 지정가 {order_price}")

            elif trade_type == 'margin':
                account_info = client_to_use.get_margin_account()
                for user_asset in account_info['userAssets']:
                    if user_asset['asset'] == symbol.replace('USDT', ''):
                        current_position_qty = float(user_asset['free'])
                        break
                if current_position_qty <= 0:
                    return jsonify({"message": f"{trade_env.upper()} {trade_type.upper()} {symbol}: 청산할 마진 잔고가 없습니다. (현재 잔고: {current_position_qty})", "order": None}), 200
                order_quantity = _round_step_size(current_position_qty, step_size)
                side = SIDE_SELL # 마진 매도는 항상 SELL (롱 포지션 청산)
                order_price = _round_step_size(best_bid - tick_size, tick_size) # 매도 시 최고 매수 호가보다 1틱 낮게 (더 공격적)
                if order_price <= 0: order_price = _round_step_size(best_bid, tick_size) # 0이하 방지
                logging.info(f"{trade_env.upper()} {trade_type.upper()} {symbol}: 마진 청산 주문 준비 (매도): 수량 {order_quantity}, 지정가 {order_price}")

            elif trade_type == 'futures':
                positions = client_to_use.futures_account()['positions']
                current_position_qty = 0.0
                for pos in positions:
                    if pos['symbol'] == symbol:
                        current_position_qty = float(pos['positionAmt'])
                        break
                
                if current_position_qty == 0:
                    return jsonify({"message": f"{trade_env.upper()} {trade_type.upper()} {symbol}: 청산할 선물 포지션이 없습니다. (현재 포지션: {current_position_qty})", "order": None}), 200
                
                order_quantity = _round_step_size(abs(current_position_qty), step_size)
                
                if current_position_qty > 0: # 롱 포지션 청산 (매도)
                    side = SIDE_SELL
                    order_price = _round_step_size(best_bid - tick_size, tick_size) # 매도 시 최고 매수 호가보다 1틱 낮게 (더 공격적)
                    if order_price <= 0: order_price = _round_step_size(best_bid, tick_size) # 0이하 방지
                    logging.info(f"{trade_env.upper()} {trade_type.upper()} {symbol}: 롱 포지션 청산 (매도) 수량 {order_quantity}, 지정가 {order_price}")
                else: # 숏 포지션 청산 (매수)
                    side = SIDE_BUY
                    order_price = _round_step_size(best_ask + tick_size, tick_size) # 매수 시 최저 매도 호가보다 1틱 높게 (더 공격적)
                    logging.info(f"{trade_env.upper()} {trade_type.upper()} {symbol}: 숏 포지션 청산 (매수) 수량 {order_quantity}, 지정가 {order_price}")
        
        else: # 일반 매수/매도 진입 로직 (order_execution_type에 따름)
            if side == SIDE_BUY:
                # 매수 수량: 거래소 최소 주문 수량 또는 11 USDT 상당 수량 중 더 작은 값
                quantity_for_11_usdt = 11.0 / current_price
                calculated_buy_qty = _round_step_size(quantity_for_11_usdt, step_size)
                
                order_quantity = min(min_qty, calculated_buy_qty) # 더 작은 값 선택
                
                # 매수 가격: 지정가인 경우 최저 매도 호가에 1 틱을 더한 가격 (더 공격적)
                if order_final_type == ORDER_TYPE_LIMIT:
                    order_price = _round_step_size(best_ask + tick_size, tick_size)
                    # 가격이 0 이하가 되는 것을 방지 (매우 낮은 가격의 코인일 경우)
                    if order_price <= 0: 
                        order_price = _round_step_size(current_price * 0.99, tick_size) # 안전하게 1% 낮은 가격으로 설정 (fallback)
                
                logging.info(f"{trade_env.upper()} {trade_type.upper()} {symbol} 매수 주문 준비: 수량 {order_quantity}, 유형 {order_final_type}, 지정가 {order_price if order_final_type == ORDER_TYPE_LIMIT else 'N/A'}")

            elif side == SIDE_SELL:
                # 매도 수량: 11 USDT 상당의 수량을 매도 (숏 포지션 진입)
                quantity_for_11_usdt = 11.0 / current_price
                order_quantity = _round_step_size(quantity_for_11_usdt, step_size)
                
                # 매도 가격: 지정가인 경우 최고 매수 호가에서 1 틱을 뺀 가격 (더 공격적)
                if order_final_type == ORDER_TYPE_LIMIT:
                    order_price = _round_step_size(best_bid - tick_size, tick_size)
                    if order_price <= 0: # 가격이 0 이하가 되는 것을 방지
                        order_price = _round_step_size(current_price * 1.01, tick_size) # 안전하게 1% 높은 가격으로 설정 (fallback)
                
                logging.info(f"{trade_env.upper()} {trade_type.upper()} {symbol} 매도 주문 준비 (숏 진입): 수량 {order_quantity}, 유형 {order_final_type}, 지정가 {order_price if order_final_type == ORDER_TYPE_LIMIT else 'N/A'}")


        # 최종 수량 유효성 검사
        is_valid_qty, msg = validate_quantity(symbol, order_quantity, trade_env, trade_type)
        if not is_valid_qty:
            logging.warning(f"최종 주문 수량 유효성 검사 실패 ({symbol}, {order_quantity}): {msg}")
            return jsonify({"error": f"최종 주문 수량 유효성 검사 실패: {msg}"}), 400
        
        # 주문 수량이 0인 경우 (예: 매도할 포지션이 없는 경우)
        if order_quantity <= 0:
            return jsonify({"message": f"{symbol}에 대한 {side} 주문을 실행할 유효한 수량이 없습니다. (현재 포지션이 0이거나 잔액 부족)","order": None}), 200


        # --- 주문 제출 ---
        order = None
        if order_final_type == ORDER_TYPE_LIMIT:
            if trade_type == 'spot':
                order = client_to_use.create_order(
                    symbol=symbol,
                    side=side,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    quantity=order_quantity,
                    price=order_price
                )
            elif trade_type == 'margin':
                order = client_to_use.create_margin_order(
                    symbol=symbol,
                    side=side,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    quantity=order_quantity,
                    price=order_price,
                    isIsolated='TRUE' # 격리 마진 사용 예시
                )
            elif trade_type == 'futures':
                order = client_to_use.create_order(
                    symbol=symbol,
                    side=side,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    quantity=order_quantity,
                    price=order_price
                )
        elif order_final_type == ORDER_TYPE_MARKET:
            if trade_type == 'spot':
                order = client_to_use.create_order(
                    symbol=symbol,
                    side=side,
                    type=ORDER_TYPE_MARKET,
                    quantity=order_quantity
                )
            elif trade_type == 'margin':
                order = client_to_use.create_margin_order(
                    symbol=symbol,
                    side=side,
                    type=ORDER_TYPE_MARKET,
                    quantity=order_quantity,
                    isIsolated='TRUE'
                )
            elif trade_type == 'futures':
                order = client_to_use.create_order(
                    symbol=symbol,
                    side=side,
                    type=ORDER_TYPE_MARKET,
                    quantity=order_quantity
                )
        
        logging.info(f"{trade_env.upper()} {trade_type.upper()} {order_final_type} 주문 성공 ({side} {order_quantity} {symbol} @ {order_price if order_final_type == ORDER_TYPE_LIMIT else '시장가'}): {order}")

        # --- 거래 내역 기록 (실제 체결 정보는 추후 확인 필요) ---
        executed_qty = float(order.get('executedQty', 0))
        cummulative_quote_qty = float(order.get('cummulativeQuoteQty', 0))
        
        fee = 0.0
        profit = 0.0
        profit_percentage = 0.0

        # 시장가 주문의 경우 체결 가격을 order.get('fills')[0]['price'] 등으로 가져와야 함
        # 여기서는 단순화를 위해 주문 가격(지정가) 또는 현재가(시장가)를 사용
        actual_trade_price = order_price if order_final_type == ORDER_TYPE_LIMIT else current_price

        if executed_qty > 0 and cummulative_quote_qty > 0:
            # 수수료율 가정 (현물 0.1%, 선물 Taker 0.04%, 마진 0.1% 가정)
            fee_rate = 0.001 # 기본 현물/마진 수수료율
            if trade_type == 'futures':
                fee_rate = 0.0004 # 선물 Taker 수수료율
            
            estimated_fee = cummulative_quote_qty * fee_rate
            fee = estimated_fee

            # 수익 계산 (간단화된 로직, 실제 봇은 체결 이력과 포지션 매칭 필요)
            if side == SIDE_SELL: # 롱 포지션 청산 (매도)
                matched_buy_trade = None
                for i in range(len(trade_history) -1, -1, -1):
                    if trade_history[i]['symbol'] == symbol and trade_history[i]['side'] == SIDE_BUY and trade_history[i]['status'] == 'FILLED' and trade_history[i]['quantity'] == executed_qty and trade_history[i]['trade_type'] == trade_type and trade_history[i]['trade_env'] == trade_env:
                        matched_buy_trade = trade_history[i]
                        break
                
                if matched_buy_trade:
                    buy_price = matched_buy_trade['price']
                    gross_profit = (actual_trade_price - buy_price) * executed_qty # 실제 체결 가격으로 계산
                    net_profit = gross_profit - fee - matched_buy_trade['fee']
                    profit = net_profit
                    if (buy_price * executed_qty) != 0:
                        profit_percentage = (net_profit / (buy_price * executed_qty)) * 100
                    logging.info(f"{trade_env.upper()} {trade_type.upper()} {symbol} 매도 수익 계산 완료: 수익금={profit}, 수익률={profit_percentage}%")
                else:
                    logging.warning(f"{trade_env.upper()} {trade_type.upper()} {symbol} 매도에 대한 매칭되는 매수 기록을 찾을 수 없습니다. 수익 계산 생략.")
            elif side == SIDE_BUY and trade_type == 'futures' and current_position_qty < 0: # 선물 숏 포지션 청산 (매수)
                matched_sell_trade = None
                for i in range(len(trade_history) -1, -1, -1):
                    if trade_history[i]['symbol'] == symbol and trade_history[i]['side'] == SIDE_SELL and trade_history[i]['status'] == 'FILLED' and trade_history[i]['quantity'] == executed_qty and trade_history[i]['trade_type'] == trade_type and trade_history[i]['trade_env'] == trade_env:
                        matched_sell_trade = trade_history[i]
                        break
                
                if matched_sell_trade:
                    sell_price = matched_sell_trade['price']
                    gross_profit = (sell_price - actual_trade_price) * executed_qty # 숏 포지션은 매도-매수 차익
                    net_profit = gross_profit - fee - matched_sell_trade['fee']
                    profit = net_profit
                    if (sell_price * executed_qty) != 0:
                        profit_percentage = (net_profit / (sell_price * executed_qty)) * 100
                    logging.info(f"{trade_env.upper()} {trade_type.upper()} {symbol} 숏 포지션 청산 수익 계산 완료: 수익금={profit}, 수익률={profit_percentage}%")
                else:
                    logging.warning(f"{trade_env.upper()} {trade_type.upper()} {symbol} 숏 포지션 청산에 대한 매칭되는 매도 기록을 찾을 수 없습니다. 수익 계산 생략.")
        else:
            logging.warning(f"주문 {order.get('orderId')} ({symbol} {side})이 체결되지 않았거나 수량/금액이 0입니다. 수익 계산 생략.")


        trade_record = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "side": side,
            "trade_type": trade_type, # 거래 유형 기록
            "trade_env": trade_env, # 거래 환경 기록
            "quantity": executed_qty, # 실제 체결 수량
            "price": actual_trade_price, # 실제 체결 가격 (지정가 또는 시장가)
            "orderId": order.get('orderId'),
            "status": order.get('status'),
            "fee": fee,
            "profit": profit,
            "profit_percentage": profit_percentage,
            "is_close_position": is_close_position, # 청산 주문 여부 기록
            "order_execution_type": order_final_type # 주문 실행 유형 기록
        }
        
        trade_history.append(trade_record)
        save_trade_history() # 변경된 내역 파일에 저장

        return jsonify({"message": f"주문 요청 성공 ({order.get('status')}): {side} {executed_qty} {symbol} @ {actual_trade_price} ({trade_env.upper()} {trade_type.upper()})", "order": order})
    
    except BinanceAPIException as e:
        logging.error(f"{trade_env.upper()} {trade_type.upper()} 주문 실패 (API 오류 - {side} {symbol}): {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}")
        if e.code in [None, -1022]:
            logging.error("API 키/시크릿, IP 화이트리스트, 서버 시간 동기화를 확인하세요.")
        elif e.code == -2019: # 마진 부족
            logging.error("마진 부족 오류: 해당 계좌에 충분한 잔고가 있는지 확인하세요.")
        elif e.code == -1013: # FILTER_FAILURE_LOT_SIZE or MIN_NOTIONAL
            logging.error("주문 수량/가격 필터 오류: 수량, 최소/최대/스텝 사이즈 또는 최소 명목 가치를 확인하세요.")
        raise
    except Exception as e:
        logging.error(f"{trade_env.upper()} {trade_type.upper()} 주문 중 알 수 없는 오류 발생 ({side} {symbol}): {e}")
        raise

@app.route('/api/account_balance', methods=['GET']) # 엔드포인트 이름 변경
@binance_api_retry_decorator
def get_account_balance(): # 함수 이름 변경
    """
    거래 유형(현물/마진/선물) 및 환경(테스트넷/실제)에 따라 계좌의 USDT 잔고를 가져옵니다.
    """
    trade_env = request.args.get('trade_env', 'futures').strip() # 공백 제거
    trade_type = request.args.get('trade_type', 'futures').strip() # 공백 제거

    client_to_use = None
    if trade_env in clients and trade_type in clients[trade_env]:
        client_to_use = clients[trade_env][trade_type]
    else:
        return jsonify({"error": "유효하지 않은 거래 환경 또는 유형입니다."}), 400

    usdt_balance = 0.0
    try:
        if trade_type == 'spot':
            account_info = client_to_use.get_account()
            for asset in account_info['balances']:
                if asset['asset'] == 'USDT':
                    usdt_balance = float(asset['free']) # 현물은 free 잔고
                    break
        elif trade_type == 'margin':
            account_info = client_to_use.get_margin_account()
            for user_asset in account_info['userAssets']:
                if user_asset['asset'] == 'USDT':
                    usdt_balance = float(user_asset['free']) # 마진은 free 잔고
                    break
        elif trade_type == 'futures':
            account_info = client_to_use.futures_account()
            for asset in account_info['assets']:
                if asset['asset'] == 'USDT':
                    usdt_balance = float(asset['walletBalance']) # 선물은 walletBalance
                    break
        else:
            return jsonify({"error": "유효하지 않은 거래 유형입니다."}), 400

        logging.info(f"{trade_env.upper()} {trade_type.upper()} 계좌 USDT 잔고 조회 성공: {usdt_balance}")
        api_status[f"{trade_env}_{trade_type}"]["status"] = "연결됨"
        api_status[f"{trade_env}_{trade_type}"]["message"] = "API 연결 정상."
        return jsonify({"asset": "USDT", "balance": usdt_balance})
    except BinanceAPIException as e:
        logging.error(f"{trade_env.upper()} {trade_type.upper()} 계좌 잔고 조회 실패 (API 오류): {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}")
        api_status[f"{trade_env}_{trade_type}"]["status"] = "오류"
        api_status[f"{trade_env}_{trade_type}"]["message"] = f"API 오류: {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}"
        if e.code in [None, -1022]:
            logging.error("API 키/시크릿, IP 화이트리스트, 시스템 시간 동기화를 확인하세요.")
        raise
    except Exception as e:
        logging.error(f"{trade_env.upper()} {trade_type.upper()} 계좌 잔고 조회 중 알 수 없는 오류 발생: {e}")
        api_status[f"{trade_env}_{trade_type}"]["status"] = "오류"
        api_status[f"{trade_env}_{trade_type}"]["message"] = f"서버 오류: {str(e)}"
        raise

@app.route('/api/all_account_balances', methods=['GET'])
@binance_api_retry_decorator
def get_all_account_balances():
    """모든 환경/유형의 USDT 잔고를 가져옵니다."""
    all_balances = {}
    environments = ['testnet', 'real']
    types = ['spot', 'futures'] # 마진은 현물 잔고와 동일하게 처리되므로 별도 조회하지 않음

    for env in environments:
        for typ in types:
            try:
                client_obj = clients[env][typ]
                usdt_balance = 0.0
                if typ == 'spot':
                    account_info = client_obj.get_account()
                    for asset in account_info['balances']:
                        if asset['asset'] == 'USDT':
                            usdt_balance = float(asset['free'])
                            break
                elif typ == 'futures':
                    account_info = client_obj.futures_account()
                    for asset in account_info['assets']:
                        if asset['asset'] == 'USDT':
                            usdt_balance = float(asset['walletBalance'])
                            break
                all_balances[f"{env}_{typ}"] = usdt_balance
                api_status[f"{env}_{typ}"]["status"] = "연결됨"
                api_status[f"{env}_{typ}"]["message"] = "API 연결 정상."
            except BinanceAPIException as e:
                logging.error(f"모든 잔고 조회 실패 ({env.upper()} {typ.upper()}): {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}")
                all_balances[f"{env}_{typ}"] = "오류"
                api_status[f"{env}_{typ}"]["status"] = "오류"
                api_status[f"{env}_{typ}"]["message"] = f"API 오류: {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}"
            except Exception as e:
                logging.error(f"모든 잔고 조회 중 알 수 없는 오류 발생 ({env.upper()} {typ.upper()}): {e}")
                all_balances[f"{env}_{typ}"] = "오류"
                api_status[f"{env}_{typ}"]["status"] = "오류"
                api_status[f"{env}_{typ}"]["message"] = f"서버 오류: {str(e)}"
    return jsonify(all_balances)


@app.route('/api/open_positions', methods=['GET'])
@binance_api_retry_decorator
def get_open_positions():
    """
    거래 유형(현물/마진/선물) 및 환경(테스트넷/실제)에 따라 현재 열려 있는 포지션들을 가져옵니다.
    현물/마진은 보유 자산 목록을 반환합니다.
    """
    trade_env = request.args.get('trade_env', 'futures').strip() # 공백 제거
    trade_type = request.args.get('trade_type', 'futures').strip() # 공백 제거
    positions = []

    client_to_use = None
    if trade_env in clients and trade_type in clients[trade_env]:
        client_to_use = clients[trade_env][trade_type]
    else:
        return jsonify({"error": "유효하지 않은 거래 환경 또는 유형입니다."}), 400

    try:
        if trade_type == 'spot':
            account_info = client_to_use.get_account()
            for asset in account_info['balances']:
                free_qty = float(asset['free'])
                if free_qty > 0 and asset['asset'] != 'USDT': # USDT 제외, 0이 아닌 잔고만 포지션으로 간주
                    positions.append({
                        "symbol": asset['asset'] + "USDT", # 예: BTC -> BTCUSDT
                        "positionAmt": free_qty,
                        "entryPrice": None,
                        "unrealizedProfit": None,
                        "liquidationPrice": None
                    })
        elif trade_type == 'margin':
            account_info = client_to_use.get_margin_account()
            for user_asset in account_info['userAssets']:
                free_qty = float(user_asset['free'])
                locked_qty = float(user_asset['locked'])
                total_qty = free_qty + locked_qty
                if total_qty > 0 and user_asset['asset'] != 'USDT': # USDT 제외, 0이 아닌 잔고만 포지션으로 간주
                    positions.append({
                        "symbol": user_asset['asset'] + "USDT",
                        "positionAmt": total_qty,
                        "entryPrice": None,
                        "unrealizedProfit": None,
                        "liquidationPrice": None
                    })
        elif trade_type == 'futures':
            account_info = client_to_use.futures_account()
            for position in account_info['positions']:
                if float(position['positionAmt']) != 0: # 포지션이 0이 아닌 것만 필터링
                    positions.append({
                        "symbol": position['symbol'],
                        "positionAmt": float(position['positionAmt']),
                        "entryPrice": float(position.get('entryPrice', 0.0)), # .get()으로 KeyError 방지
                        "unrealizedProfit": float(position.get('unRealizedProfit', 0.0)), # .get()으로 KeyError 방지
                        "liquidationPrice": float(position.get('liquidationPrice', 0.0)) if position.get('liquidationPrice') else None # .get()으로 KeyError 방지
                    })
        else:
            return jsonify({"error": "유효하지 않은 거래 유형입니다."}), 400

        logging.info(f"{trade_env.upper()} {trade_type.upper()} 열린 포지션 조회 성공: {len(positions)}개 포지션.")
        api_status[f"{trade_env}_{trade_type}"]["status"] = "연결됨"
        api_status[f"{trade_env}_{trade_type}"]["message"] = "API 연결 정상."
        return jsonify(positions)
    except BinanceAPIException as e:
        logging.error(f"{trade_env.upper()} {trade_type.upper()} 열린 포지션 조회 실패 (API 오류): {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}")
        api_status[f"{trade_env}_{trade_type}"]["status"] = "오류"
        api_status[f"{trade_env}_{trade_type}"]["message"] = f"API 오류: {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}"
        if e.code in [None, -1022]:
            logging.error("API 키/시크릿, IP 화이트리스트, 시스템 시간 동기화를 확인하세요.")
        raise
    except Exception as e:
        logging.error(f"{trade_env.upper()} {trade_type.upper()} 열린 포지션 조회 중 알 수 없는 오류 발생: {e}")
        api_status[f"{trade_env}_{trade_type}"]["status"] = "오류"
        api_status[f"{trade_env}_{trade_type}"]["message"] = f"서버 오류: {str(e)}"
        raise

@app.route('/api/trade_history', methods=['GET'])
def get_trade_history_and_performance():
    """
    저장된 거래 내역과 누적 수익 정보를 반환합니다.
    """
    cumulative_profit = 0.0
    
    # 펀딩비와 같은 기타 수입/지출을 가져와서 누적 수익에 반영 (실제 선물 계좌에서만)
    total_funding_fees_testnet = 0.0
    total_funding_fees_real = 0.0

    try:
        income_history_testnet = clients['testnet']['futures'].futures_income_history()
        for income in income_history_testnet:
            if income['incomeType'] == 'FUNDING_FEE':
                total_funding_fees_testnet += float(income['income'])
    except BinanceAPIException as e:
        logging.warning(f"테스트넷 펀딩비 내역 조회 실패 (API 오류): {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}")
    except Exception as e:
        logging.warning(f"테스트넷 펀딩비 내역 조회 중 알 수 없는 오류 발생: {e}")

    try:
        income_history_real = clients['real']['futures'].futures_income_history()
        for income in income_history_real:
            if income['incomeType'] == 'FUNDING_FEE':
                total_funding_fees_real += float(income['income'])
    except BinanceAPIException as e:
        logging.warning(f"실제 펀딩비 내역 조회 실패 (API 오류): {e.status_code if e.status_code else '알 수 없음'} - {e.message if e.message else '메시지 없음'}")
    except Exception as e:
        logging.warning(f"실제 펀딩비 내역 조회 중 알 수 없는 오류 발생: {e}")

    total_realized_pnl_from_trades = 0.0
    total_invested_for_pnl_calc = 0.0 # 수익률 계산을 위한 투자금 (간단화)

    for trade in trade_history:
        total_realized_pnl_from_trades += trade.get('profit', 0.0)
        # 매수 거래의 금액만 수익률 계산을 위한 투자금으로 간주 (단순화)
        if trade['side'] == SIDE_BUY and trade.get('status') == 'FILLED':
            total_invested_for_pnl_calc += trade['quantity'] * trade['price']

    # 펀딩비를 포함한 최종 누적 수익
    cumulative_profit = total_realized_pnl_from_trades + total_funding_fees_testnet + total_funding_fees_real

    cumulative_return_percentage = 0.0
    # 펀딩비는 거래 원금에 직접 연결되지 않으므로, 투자금액은 실제 매수 금액만 사용
    if total_invested_for_pnl_calc > 0:
        # 수익률 계산 시 펀딩비도 포함된 총 수익을 사용
        cumulative_return_percentage = (cumulative_profit / total_invested_for_pnl_calc) * 100
    
    return jsonify({
        "history": trade_history,
        "cumulative": {
            "profit": cumulative_profit,
            "return_percentage": cumulative_return_percentage,
            "total_funding_fees_testnet": total_funding_fees_testnet,
            "total_funding_fees_real": total_funding_fees_real
        }
    })


if __name__ == '__main__':
    logging.info("Flask 서버 시작 중...")
    app.run(host='0.0.0.0', port=5000, debug=True)
