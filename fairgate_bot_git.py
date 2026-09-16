import os
import re
import json
import time
import hmac
import math
import hashlib
import logging
import threading
import traceback
import urllib.parse
import subprocess
import argparse
from datetime import datetime, timedelta

import requests
import socks
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telethon import TelegramClient, events
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('gate_bot.log', encoding='utf-8'),
    ]
)
log = logging.getLogger('GateBot')

PM2_APP_NAME = os.getenv('PM2_APP_NAME', '19')

TG_API_ID = int(os.getenv('TG_API_ID', '0'))
TG_API_HASH = os.getenv('TG_API_HASH', '')
TG_PHONE = os.getenv('TG_PHONE', '')
SESSION_FILE = os.getenv('SESSION_FILE', 'Trading_Bot_Fair_Gate.session')

LOG_BOT_TOKEN = os.getenv('LOG_BOT_TOKEN', '')
LOG_CHAT_ID = int(os.getenv('LOG_CHAT_ID', '0'))

GATE_API_KEY = os.getenv('GATE_API_KEY', '')
GATE_API_SECRET = os.getenv('GATE_API_SECRET', '')
GATE_BASE_URL = 'https://fx-api.gateio.ws'
GATE_API_PREFIX = '/api/v4'
SETTLE = 'usdt'

PROXY_HOST = os.getenv('PROXY_HOST', '')
PROXY_PORT = int(os.getenv('PROXY_PORT', '0'))
PROXY_USER = os.getenv('PROXY_USER', '')
PROXY_PASS = os.getenv('PROXY_PASS', '')

if PROXY_HOST and PROXY_PORT:
    if PROXY_USER and PROXY_PASS:
        HTTP_PROXIES = {
            'http': f'http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}',
            'https': f'http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}',
        }
        TELETHON_PROXY = (
            socks.HTTP,
            PROXY_HOST,
            PROXY_PORT,
            True,
            PROXY_USER,
            PROXY_PASS
        )
    else:
        HTTP_PROXIES = {
            'http': f'http://{PROXY_HOST}:{PROXY_PORT}',
            'https': f'http://{PROXY_HOST}:{PROXY_PORT}',
        }
        TELETHON_PROXY = (
            socks.HTTP,
            PROXY_HOST,
            PROXY_PORT
        )
else:
    HTTP_PROXIES = {}
    TELETHON_PROXY = None

parser = argparse.ArgumentParser()

parser.add_argument("--usdt", type=float)
parser.add_argument("--leverage", type=int)

args = parser.parse_args()

POSITION_USDT = float(os.getenv('POSITION_USDT', '5'))
LEVERAGE = int(os.getenv('LEVERAGE', '5'))

if args.usdt is not None:
    POSITION_USDT = args.usdt

if args.leverage is not None:
    LEVERAGE = args.leverage

TP_BUFFER_PCT = float(os.getenv('TP_BUFFER_PCT', '0.02'))
SL_PCT = float(os.getenv('SL_PCT', '0.03'))
INTER_TP_THRESHOLD = float(os.getenv('INTER_TP_THRESHOLD', '15'))
INTER_TP_SL_PCT = float(os.getenv('INTER_TP_SL_PCT', '0.03'))
MAX_OPEN_POSITIONS = int(os.getenv('MAX_OPEN_POSITIONS', '10'))
MARGIN_USAGE_PCT = float(os.getenv('MARGIN_USAGE_PCT', '0.85'))
MIN_MARGIN_LEFT = float(os.getenv('MIN_MARGIN_LEFT', '0.25'))
SPAM_COOLDOWN_SEC = int(os.getenv('SPAM_COOLDOWN_SEC', '60'))
SIGNAL_MAX_AGE_SEC = int(os.getenv('SIGNAL_MAX_AGE_SEC', '30'))
BANLIST_DAYS = int(os.getenv('BANLIST_DAYS', '3'))
MAX_LOCAL_LOSSES = int(os.getenv('MAX_LOCAL_LOSSES', '2'))

BANLIST_FILE = 'global_banlist_gate.json'

bot = TelegramClient(
    SESSION_FILE,
    TG_API_ID,
    TG_API_HASH,
    proxy=TELETHON_PROXY
)

ACTIVE_POSITIONS = {}
position_lock = threading.Lock()
OPENING_IN_PROGRESS = set()
TPSL_PLACING = set()
_last_signal_ts = {}
local_banlist = {}
global_banlist = set()
_contract_cache = {}
_log_buffer = {}
PNL_PENDING = {}
PNL_LOCK = threading.Lock()
_sync_last_alive = time.time()
_sync_thread = None
_name_to_symbol_cache = {}


def load_banlist():
    global global_banlist

    if not os.path.exists(BANLIST_FILE):
        return

    try:
        with open(BANLIST_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        saved_time = datetime.fromisoformat(data['saved_at'])

        if datetime.now() - saved_time > timedelta(days=BANLIST_DAYS):
            global_banlist = set()
            save_banlist()
        else:
            global_banlist = set(data.get('symbols', []))
            log.info('BANLIST LOADED: %s', global_banlist)

    except Exception as e:
        log.error('load_banlist error: %s', e)


def save_banlist():
    try:
        with open(BANLIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(
                {
                    'saved_at': datetime.now().isoformat(),
                    'symbols': list(global_banlist)
                },
                f
            )
    except Exception as e:
        log.error('save_banlist error: %s', e)


load_banlist()


def to_gate(symbol: str) -> str:
    return symbol.replace('-', '_')


def to_bingx(symbol: str) -> str:
    return symbol.replace('_', '-')


def _make_session() -> requests.Session:
    s = requests.Session()

    if HTTP_PROXIES:
        s.proxies.update(HTTP_PROXIES)

    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504]
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=20
    )

    s.mount('http://', adapter)
    s.mount('https://', adapter)

    return s


_session = _make_session()


def _gen_sign(
    method: str,
    path: str,
    query: str = '',
    body: str = ''
) -> dict:
    t = time.time()

    hashed_body = hashlib.sha512(
        (body or '').encode()
    ).hexdigest()

    payload = (
        f'{method}\n'
        f'{path}\n'
        f'{query or ""}\n'
        f'{hashed_body}\n'
        f'{t}'
    )

    sign = hmac.new(
        GATE_API_SECRET.encode(),
        payload.encode(),
        hashlib.sha512
    ).hexdigest()

    return {
        'KEY': GATE_API_KEY,
        'Timestamp': str(t),
        'SIGN': sign
    }


def gate_request(
    path: str,
    method: str = 'GET',
    params: dict = None,
    body: dict = None,
    timeout: int = 10
):
    global _session

    try:
        full_path = GATE_API_PREFIX + path
        url = GATE_BASE_URL + full_path

        query_string = urllib.parse.urlencode(params or {})

        body_string = (
            json.dumps(body, separators=(',', ':'))
            if body else ''
        )

        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            **_gen_sign(
                method,
                full_path,
                query_string,
                body_string
            ),
        }

        full_url = url + (
            '?' + query_string
            if query_string
            else ''
        )

        resp = _session.request(
            method,
            full_url,
            headers=headers,
            data=body_string or None,
            timeout=timeout
        )

        if not resp.content:
            return {}

        if resp.status_code not in (200, 201):
            log.warning(
                'Gate %s %s -> %d: %s',
                method,
                path,
                resp.status_code,
                resp.text[:500]
            )

        return resp.json()

    except Exception as e:
        log.error(
            'gate_request error (%s %s): %s',
            method,
            path,
            e
        )

        if (
            'ConnectionReset' in str(e)
            or 'timed out' in str(e)
        ):
            _session = _make_session()

        return {}


def tg_log(
    symbol: str,
    message: str,
    send_now: bool = False
):
    _log_buffer.setdefault(symbol, [])

    _log_buffer[symbol].append(
        f'[{datetime.now().strftime("%H:%M:%S")}] {message}'
    )

    log.info('[%s] %s', symbol, message)

    if send_now:
        text = '\n'.join(_log_buffer[symbol])
        _log_buffer[symbol].clear()
        _send_tg_log(symbol, text)


def _send_tg_log(symbol: str, text: str):
    try:
        if not LOG_BOT_TOKEN or not LOG_CHAT_ID:
            return

        url = (
            f'https://api.telegram.org/'
            f'bot{LOG_BOT_TOKEN}/sendMessage'
        )

        data = {
            'chat_id': LOG_CHAT_ID,
            'text': f'[{symbol}]\n{text}'[:4096],
            'parse_mode': 'HTML'
        }

        requests.post(
            url,
            data=data,
            proxies=HTTP_PROXIES or None,
            timeout=15
        )

    except Exception as e:
        log.error('tg_log send error: %s', e)


def get_contract_info(symbol: str) -> dict:
    if symbol not in _contract_cache:
        resp = gate_request(
            f'/futures/{SETTLE}/contracts/{symbol}'
        )

        if (
            isinstance(resp, dict)
            and resp
            and not resp.get('label')
        ):
            _contract_cache[symbol] = resp
        else:
            log.warning(
                '[%s] get_contract_info failed: %s',
                symbol,
                resp
            )

    return _contract_cache.get(symbol, {})


def get_price_step(symbol: str) -> float:
    info = get_contract_info(symbol)

    try:
        return float(
            info.get('order_price_round', '0.0001')
            or 0.0001
        )
    except Exception:
        return 0.0001


def get_order_size_min(symbol: str) -> int:
    info = get_contract_info(symbol)

    try:
        return max(
            1,
            int(float(info.get('order_size_min', 1)))
        )
    except Exception:
        return 1


def get_order_size_max(symbol: str) -> int:
    info = get_contract_info(symbol)
    raw = info.get('order_size_max')

    if raw in (None, '', 0, '0'):
        return 10**12

    try:
        value = int(float(raw))
        return value if value > 0 else 10**12
    except Exception:
        return 10**12


def get_market_order_size_max(symbol: str) -> int:
    info = get_contract_info(symbol)

    raw = (
        info.get('market_order_size_max')
        or info.get('order_size_max')
    )

    if raw in (None, '', 0, '0'):
        return 35000

    try:
        value = int(float(raw))
        return value if value > 0 else 35000
    except Exception:
        return 35000


def round_price(price: float, step: float) -> float:
    if step <= 0:
        return round(price, 6)

    step_str = (
        f'{step:.12f}'
        .rstrip('0')
        .rstrip('.')
    )

    decimals = (
        len(step_str.split('.')[1])
        if '.' in step_str
        else 0
    )

    return round(
        round(price / step) * step,
        decimals
    )


def get_available_margin() -> float:
    resp = gate_request(
        f'/futures/{SETTLE}/accounts'
    )

    if not isinstance(resp, dict):
        return 0.0

    for key in (
        'available',
        'available_balance',
        'balance_available'
    ):
        try:
            value = resp.get(key)

            if value is not None:
                parsed = float(value)

                if parsed >= 0:
                    log.info(
                        'Available margin [%s]=%.8f | raw=%s',
                        key,
                        parsed,
                        resp
                    )

                    return parsed

        except Exception:
            pass

    log.warning(
        'Cannot parse available margin from account response: %s',
        resp
    )

    return 0.0


def get_contract_multiplier(symbol: str) -> float:
    info = get_contract_info(symbol)

    try:
        multiplier = abs(
            float(info.get('multiplier', 1) or 1)
        )

        return (
            multiplier
            if multiplier > 0
            else 1.0
        )

    except Exception:
        return 1.0


def estimate_margin_for_order(
    symbol: str,
    qty: int,
    price: float,
    leverage: int
) -> float:
    multiplier = get_contract_multiplier(symbol)

    if (
        qty <= 0
        or price <= 0
        or leverage <= 0
        or multiplier <= 0
    ):
        return 0.0

    notional = qty * price * multiplier
    margin = notional / leverage

    log.info(
        'Margin estimate | symbol=%s qty=%s price=%.8f '
        'multiplier=%.12f lev=%s notional=%.8f margin=%.8f',
        symbol,
        qty,
        price,
        multiplier,
        leverage,
        notional,
        margin
    )

    return margin


def max_affordable_contracts(
    symbol: str,
    available_margin: float,
    price: float,
    leverage: int
) -> int:
    multiplier = get_contract_multiplier(symbol)

    if (
        available_margin <= 0
        or price <= 0
        or leverage <= 0
        or multiplier <= 0
    ):
        return 0

    usable_margin = (
        max(
            0.0,
            available_margin - MIN_MARGIN_LEFT
        )
        * MARGIN_USAGE_PCT
    )

    one_contract_margin = (
        price * multiplier
    ) / leverage

    if one_contract_margin <= 0:
        return 0

    qty = math.floor(
        usable_margin / one_contract_margin
    )

    min_qty = get_order_size_min(symbol)

    max_qty = min(
        get_order_size_max(symbol),
        get_market_order_size_max(symbol)
    )

    if qty < min_qty:
        log.warning(
            '[%s] Not enough margin even for min qty: '
            'available=%.8f usable=%.8f '
            'one_contract_margin=%.8f min_qty=%s',
            symbol,
            available_margin,
            usable_margin,
            one_contract_margin,
            min_qty
        )

        return 0

    return int(
        max(
            min_qty,
            min(qty, max_qty)
        )
    )


def get_current_price(symbol: str) -> float:
    resp = gate_request(
        f'/futures/{SETTLE}/tickers',
        params={'contract': symbol}
    )

    try:
        if isinstance(resp, list) and resp:
            return float(
                resp[0].get('last', 0)
            )
    except Exception:
        pass

    return 0.0


def get_position_size(
    symbol: str,
    side: str
) -> float:
    resp = gate_request(
        f'/futures/{SETTLE}/dual_comp/positions/{symbol}'
    )

    if not isinstance(resp, list):
        log.warning(
            '[%s] get_position_size unexpected response for %s: %s',
            symbol,
            side,
            resp
        )
        return 0.0

    for p in resp:
        try:
            mode = str(
                p.get('mode', '')
            ).lower()

            size = float(
                p.get('size', 0)
            )

            if (
                side == 'long'
                and 'long' in mode
                and size != 0
            ):
                return abs(size)

            if (
                side == 'short'
                and 'short' in mode
                and size != 0
            ):
                return abs(size)

        except Exception as e:
            log.warning(
                '[%s] get_position_size parse error: %s in %s',
                symbol,
                e,
                p
            )

    return 0.0


def get_avg_price(
    symbol: str,
    side: str,
    retries: int = 6
) -> float:
    for _ in range(retries):
        resp = gate_request(
            f'/futures/{SETTLE}/dual_comp/positions/{symbol}'
        )

        if isinstance(resp, list):
            for p in resp:
                try:
                    mode = str(
                        p.get('mode', '')
                    ).lower()

                    size = float(
                        p.get('size', 0)
                    )

                    if (
                        side == 'long'
                        and 'long' in mode
                        and size != 0
                    ):
                        return float(
                            p.get('entry_price', 0)
                        )

                    if (
                        side == 'short'
                        and 'short' in mode
                        and size != 0
                    ):
                        return float(
                            p.get('entry_price', 0)
                        )

                except Exception:
                    pass

        time.sleep(0.5)

    return 0.0


def contracts_from_usdt(
    symbol: str,
    position_usdt: float,
    price: float,
    leverage: int
) -> int:
    multiplier = get_contract_multiplier(symbol)

    if (
        position_usdt <= 0
        or price <= 0
        or leverage <= 0
        or multiplier <= 0
    ):
        return 0

    target_notional = position_usdt * leverage
    one_contract_notional = price * multiplier

    if one_contract_notional <= 0:
        return 0

    qty = math.floor(
        target_notional / one_contract_notional
    )

    min_qty = get_order_size_min(symbol)

    max_qty = min(
        get_order_size_max(symbol),
        get_market_order_size_max(symbol)
    )

    if qty < min_qty:
        qty = min_qty

    qty = min(qty, max_qty)

    available = get_available_margin()

    affordable_qty = max_affordable_contracts(
        symbol,
        available,
        price,
        leverage
    )

    if affordable_qty <= 0:
        log.warning(
            '[%s] contracts_from_usdt: no affordable qty | '
            'requested=%s available=%.8f',
            symbol,
            qty,
            available
        )

        return 0

    if qty > affordable_qty:
        log.warning(
            '[%s] contracts_from_usdt reduce qty: '
            'requested=%s affordable=%s available=%.8f',
            symbol,
            qty,
            affordable_qty,
            available
        )

        qty = affordable_qty

    log.info(
        'contracts_from_usdt | symbol=%s position_usdt=%.8f '
        'lev=%s price=%.8f multiplier=%.12f '
        'target_notional=%.8f qty=%s affordable=%s',
        symbol,
        position_usdt,
        leverage,
        price,
        multiplier,
        target_notional,
        qty,
        affordable_qty
    )

    return int(qty)


def set_leverage(symbol: str, leverage: int):
    resp = gate_request(
        f'/futures/{SETTLE}/dual_comp/positions/{symbol}/leverage',
        method='POST',
        params={'leverage': leverage}
    )

    if isinstance(resp, dict) and resp.get('label'):
        log.warning(
            '[%s] dual_comp set_leverage failed, fallback: %s',
            symbol,
            resp
        )

        resp = gate_request(
            f'/futures/{SETTLE}/positions/{symbol}/leverage',
            method='POST',
            params={'leverage': leverage}
        )

    log.info(
        '[%s] set_leverage(%s) -> %s',
        symbol,
        leverage,
        resp
    )

    return resp


def place_market_order(
    symbol: str,
    side: str,
    size: int
) -> dict:
    body = {
        'contract': symbol,
        'size': size,
        'price': '0',
        'tif': 'ioc',
        'is_dual_mode': True
    }

    log.info('MARKET ORDER: %s', body)

    resp = gate_request(
        f'/futures/{SETTLE}/orders',
        method='POST',
        body=body
    )

    return (
        resp
        if isinstance(resp, dict)
        else {}
    )


def is_insufficient_margin_error(
    resp: dict
) -> bool:
    if not isinstance(resp, dict):
        return False

    label = str(
        resp.get('label', '')
    ).upper()

    message = str(
        resp.get('message', '')
    ).lower()

    return (
        'INSUFFICIENT_AVAILABLE' in label
        or (
            'available' in message
            and 'margin' in message
        )
    )


def parse_required_available_from_gate_error(
    resp: dict
):
    if not isinstance(resp, dict):
        return None, None

    msg = str(
        resp.get('message', '')
    )

    m = re.search(
        r'margin\s+([0-9.]+)\s+while\s+available\s+([0-9.]+)',
        msg,
        re.IGNORECASE
    )

    if not m:
        return None, None

    try:
        return (
            float(m.group(1)),
            float(m.group(2))
        )
    except Exception:
        return None, None


def reduce_qty_by_margin_error(
    qty: int,
    resp: dict,
    min_qty: int
) -> int:
    required, available = (
        parse_required_available_from_gate_error(resp)
    )

    if (
        required
        and available
        and required > 0
    ):
        new_qty = math.floor(
            qty
            * (available / required)
            * 0.85
        )
    else:
        new_qty = math.floor(
            qty * 0.6
        )

    if new_qty >= qty:
        new_qty = qty - 1

    if new_qty < min_qty:
        return 0

    return int(new_qty)


def place_market_order_safely(
    symbol: str,
    side: str,
    qty: int,
    leverage: int,
    ref_price: float
):
    min_qty = get_order_size_min(symbol)

    max_qty = min(
        get_order_size_max(symbol),
        get_market_order_size_max(symbol)
    )

    fresh_price = (
        get_current_price(symbol)
        or ref_price
    )

    available = get_available_margin()

    affordable_qty = max_affordable_contracts(
        symbol,
        available,
        fresh_price,
        leverage
    )

    if affordable_qty <= 0:
        return {
            'label': 'INSUFFICIENT_AVAILABLE_LOCAL',
            'message': (
                f'available={available:.8f} '
                f'not enough for min_qty={min_qty} '
                f'price={fresh_price:.8f} '
                f'leverage={leverage}'
            )
        }, 0

    current_qty = max(
        min_qty,
        qty
    )

    current_qty = min(
        current_qty,
        max_qty,
        affordable_qty
    )

    last_resp = {}

    for attempt in range(8):
        fresh_price = (
            get_current_price(symbol)
            or ref_price
        )

        available = get_available_margin()

        affordable_qty = max_affordable_contracts(
            symbol,
            available,
            fresh_price,
            leverage
        )

        if affordable_qty <= 0:
            return {
                'label': 'INSUFFICIENT_AVAILABLE_LOCAL',
                'message': (
                    f'available={available:.8f} '
                    f'not enough for min_qty={min_qty} '
                    f'price={fresh_price:.8f} '
                    f'leverage={leverage}'
                )
            }, 0

        current_qty = min(
            current_qty,
            affordable_qty,
            max_qty
        )

        if current_qty < min_qty:
            return {
                'label': 'INVALID_PARAM_VALUE',
                'message': (
                    f'calculated qty={current_qty} '
                    f'lower than min_qty={min_qty}'
                )
            }, 0

        signed_size = (
            current_qty
            if side == 'long'
            else -current_qty
        )

        est_margin = estimate_margin_for_order(
            symbol,
            current_qty,
            fresh_price,
            leverage
        )

        log.info(
            '[%s] Open attempt %d: qty=%s '
            'signed_size=%s price=%.8f '
            'est_margin=%.6f available=%.6f '
            'min_qty=%s max_qty=%s affordable=%s',
            symbol,
            attempt + 1,
            current_qty,
            signed_size,
            fresh_price,
            est_margin,
            available,
            min_qty,
            max_qty,
            affordable_qty
        )

        last_resp = place_market_order(
            symbol,
            side,
            signed_size
        )

        if last_resp.get('id'):
            return last_resp, current_qty

        label = (
            str(last_resp.get('label', '')).upper()
            if isinstance(last_resp, dict)
            else ''
        )

        if is_insufficient_margin_error(last_resp):
            new_qty = reduce_qty_by_margin_error(
                current_qty,
                last_resp,
                min_qty
            )

            log.warning(
                '[%s] insufficient margin: qty=%s -> %s resp=%s',
                symbol,
                current_qty,
                new_qty,
                last_resp
            )

            if new_qty <= 0:
                return last_resp, 0

            current_qty = min(
                new_qty,
                max_qty
            )

            time.sleep(0.8)
            continue

        if 'MARKET_ORDER_SIZE_TOO_LARGE' in label:
            msg = str(
                last_resp.get('message', '')
            )

            limit_match = re.search(
                r'limit\s+(\d+)',
                msg
            )

            hard_limit = (
                int(limit_match.group(1))
                if limit_match
                else int(current_qty * 0.7)
            )

            new_qty = min(
                current_qty - 1,
                hard_limit
            )

            if new_qty >= min_qty:
                current_qty = new_qty
                max_qty = min(
                    max_qty,
                    hard_limit
                )

                time.sleep(0.5)
                continue

            return last_resp, 0

        if label == 'INVALID_PARAM_VALUE':
            msg = str(
                last_resp.get('message', '')
            ).lower()

            if (
                'order_size_min' in msg
                or 'order_size_max' in msg
                or 'between' in msg
            ):
                current_qty = min(
                    current_qty,
                    max_qty
                )

                if current_qty >= min_qty:
                    time.sleep(0.5)
                    continue

        break

    return last_resp, 0


def close_position_market(
    symbol: str,
    side: str,
    qty: int
) -> dict:
    close_size = (
        -qty
        if side == 'long'
        else qty
    )

    body = {
        'contract': symbol,
        'size': close_size,
        'price': '0',
        'tif': 'ioc',
        'is_dual_mode': True,
        'reduce_only': True
    }

    resp = gate_request(
        f'/futures/{SETTLE}/orders',
        method='POST',
        body=body
    )

    return (
        resp
        if isinstance(resp, dict)
        else {}
    )


def cancel_order(order_id):
    return gate_request(
        f'/futures/{SETTLE}/orders/{order_id}',
        method='DELETE'
    )


def get_open_orders(symbol: str) -> list:
    resp = gate_request(
        f'/futures/{SETTLE}/orders',
        params={
            'contract': symbol,
            'status': 'open'
        }
    )

    return (
        resp
        if isinstance(resp, list)
        else []
    )


def place_price_order(
    symbol: str,
    side: str,
    trigger: float,
    qty: int,
    order_type: str
) -> dict:
    step = get_price_step(symbol)
    trigger = round_price(
        trigger,
        step
    )

    if side == 'long':
        rule = (
            1
            if order_type == 'tp'
            else 2
        )
        auto_size = 'close_long'
    else:
        rule = (
            2
            if order_type == 'tp'
            else 1
        )
        auto_size = 'close_short'

    body = {
        'initial': {
            'contract': symbol,
            'size': 0,
            'price': '0',
            'tif': 'ioc',
            'auto_size': auto_size,
            'text': (
                f't-{order_type}-{int(time.time())}'
            ),
        },
        'trigger': {
            'strategy_type': 0,
            'price_type': 0,
            'price': str(trigger),
            'rule': rule,
        },
    }

    log.info(
        '[%s] PRICE ORDER %s side=%s trigger=%s '
        'rule=%s auto_size=%s body=%s',
        symbol,
        order_type,
        side,
        trigger,
        rule,
        auto_size,
        body
    )

    resp = gate_request(
        f'/futures/{SETTLE}/price_orders',
        method='POST',
        body=body
    )

    if (
        isinstance(resp, dict)
        and resp.get('label')
    ):
        log.warning(
            '[%s] PRICE ORDER FAILED %s: %s',
            symbol,
            order_type,
            resp
        )

    return (
        resp
        if isinstance(resp, dict)
        else {}
    )


def cancel_price_order(order_id):
    return gate_request(
        f'/futures/{SETTLE}/price_orders/{order_id}',
        method='DELETE'
    )


def get_open_price_orders(symbol: str) -> list:
    resp = gate_request(
        f'/futures/{SETTLE}/price_orders',
        params={
            'contract': symbol,
            'status': 'open'
        }
    )

    return (
        resp
        if isinstance(resp, list)
        else []
    )


def cancel_all_price_orders(symbol: str):
    orders = get_open_price_orders(symbol)

    threads = []

    for o in orders:
        if (
            isinstance(o, dict)
            and o.get('id')
        ):
            t = threading.Thread(
                target=cancel_price_order,
                args=(o['id'],),
                daemon=True
            )

            threads.append(t)
            t.start()

    for t in threads:
        t.join()

    log.info(
        '[%s] Cancelled %d price orders',
        symbol,
        len(orders)
    )


def get_realised_pnl(
    gate_symbol: str,
    open_time: float
):
    open_time_floor = open_time - 300

    resp = gate_request(
        f'/futures/{SETTLE}/position_close',
        params={
            'contract': gate_symbol,
            'limit': 50
        }
    )

    if not isinstance(resp, list):
        log.info(
            '[%s] PNL: position_close not list: %s',
            gate_symbol,
            resp
        )

        return None

    normalized = []

    for rec in resp:
        raw_close_ts = rec.get(
            'time',
            rec.get('close_time', 0)
        )

        try:
            close_ts = float(
                raw_close_ts
            )

            if close_ts > 10**12:
                close_ts /= 1000.0

        except Exception:
            close_ts = 0.0

        normalized.append(
            (close_ts, rec)
        )

    relevant = [
        rec
        for close_ts, rec in normalized
        if close_ts >= open_time_floor
    ]

    if not relevant:
        return None

    total = 0.0
    used = 0

    for rec in relevant:
        raw = rec.get('realised_pnl')

        if raw in (None, ''):
            raw = rec.get('pnl')

        if raw in (None, ''):
            raw = '0'

        try:
            total += float(raw)
            used += 1
        except Exception:
            pass

    log.info(
        '[%s] PNL total %.4f used=%d rows=%d',
        gate_symbol,
        total,
        used,
        len(resp)
    )

    return total


def get_last_close_pnl_from_positions(
    gate_symbol: str,
    side: str = None
):
    resp = gate_request(
        f'/futures/{SETTLE}/dual_comp/positions/{gate_symbol}'
    )

    if not isinstance(resp, list):
        return None

    candidates = []

    for p in resp:
        mode = str(
            p.get('mode', '')
        ).lower()

        if (
            side == 'long'
            and 'long' not in mode
        ):
            continue

        if (
            side == 'short'
            and 'short' not in mode
        ):
            continue

        try:
            candidates.append(
                float(
                    p.get('last_close_pnl')
                )
            )
        except Exception:
            pass

    return (
        candidates[0]
        if candidates
        else None
    )


def force_ban(
    gate_symbol: str,
    bx_ticker: str,
    reason: str
):
    global_banlist.add(gate_symbol)
    save_banlist()

    tg_log(
        bx_ticker,
        f'{gate_symbol} -> GLOBAL BANLIST ({reason})',
        send_now=True
    )


def record_pnl(
    gate_symbol: str,
    open_time: float
):
    schedule_pnl_check(
        gate_symbol,
        to_bingx(gate_symbol),
        open_time,
        side=None
    )


def cleanup_stale_position(
    ticker: str,
    reason: str = 'stale position detected'
):
    gate_sym = to_gate(ticker)

    with position_lock:
        data = ACTIVE_POSITIONS.pop(
            ticker,
            None
        )

    if not data:
        return False

    open_time = data.get(
        'open_time',
        0.0
    )

    side = data.get(
        'positionSide'
    )

    try:
        cancel_all_price_orders(
            gate_sym
        )
    except Exception as e:
        log.warning(
            '[%s] cleanup cancel_all_price_orders error: %s',
            ticker,
            e
        )

    schedule_pnl_check(
        gate_sym,
        ticker,
        open_time,
        side
    )

    tg_log(
        ticker,
        f'Локальная позиция снята: {reason}. '
        f'Запрашиваю PNL...',
        send_now=True
    )

    return True


def schedule_pnl_check(
    gate_symbol: str,
    ticker: str,
    open_time: float,
    side: str = None
):
    key = (
        f'{gate_symbol}:'
        f'{open_time}:'
        f'{side or "unknown"}'
    )

    with PNL_LOCK:
        if key in PNL_PENDING:
            return

        PNL_PENDING[key] = {
            'gate_symbol': gate_symbol,
            'ticker': ticker,
            'open_time': open_time,
            'side': side,
            'created_at': time.time(),
            'attempts': 0,
        }


def pnl_pending_worker():
    while True:
        try:
            time.sleep(3)

            with PNL_LOCK:
                pending_items = list(
                    PNL_PENDING.items()
                )

            for key, item in pending_items:
                gate_symbol = item['gate_symbol']
                ticker = item['ticker']
                open_time = item['open_time']
                side = item.get('side')
                attempts = item['attempts']

                if attempts >= 20:
                    tg_log(
                        ticker,
                        'PNL: не удалось получить данные '
                        'закрытия после многих попыток',
                        send_now=True
                    )

                    with PNL_LOCK:
                        PNL_PENDING.pop(
                            key,
                            None
                        )

                    continue

                profit = get_realised_pnl(
                    gate_symbol,
                    open_time
                )

                if profit is None:
                    fallback_profit = (
                        get_last_close_pnl_from_positions(
                            gate_symbol,
                            side
                        )
                    )

                    if fallback_profit is not None:
                        profit = fallback_profit

                with PNL_LOCK:
                    if key in PNL_PENDING:
                        PNL_PENDING[key]['attempts'] += 1

                if profit is None:
                    continue

                if profit > 0:
                    local_banlist.pop(
                        ticker,
                        None
                    )

                    tg_log(
                        ticker,
                        f'PNL: {profit:+.4f} USDT '
                        f'(счётчик сброшен)',
                        send_now=True
                    )

                elif profit < 0:
                    local_banlist[ticker] = (
                        local_banlist.get(ticker, 0) + 1
                    )

                    count = local_banlist[ticker]

                    tg_log(
                        ticker,
                        f'PNL: {profit:+.4f} USDT '
                        f'убыток #{count}',
                        send_now=True
                    )

                    if count >= MAX_LOCAL_LOSSES:
                        force_ban(
                            gate_symbol,
                            ticker,
                            f'убытки подряд: {count}'
                        )

                        local_banlist.pop(
                            ticker,
                            None
                        )

                else:
                    tg_log(
                        ticker,
                        f'PNL: {profit:+.4f} USDT (ноль)',
                        send_now=True
                    )

                with PNL_LOCK:
                    PNL_PENDING.pop(
                        key,
                        None
                    )

        except Exception as e:
            log.error(
                'pnl_pending_worker error: %s\n%s',
                e,
                traceback.format_exc()
            )


def force_close_and_ban(
    gate_sym: str,
    bx_ticker: str,
    side: str,
    reason: str
):
    qty = int(
        get_position_size(
            gate_sym,
            side
        )
    )

    cancel_all_price_orders(
        gate_sym
    )

    if qty > 0:
        result = close_position_market(
            gate_sym,
            side,
            qty
        )

        tg_log(
            bx_ticker,
            f'Принудительное закрытие ({reason}): '
            f'{result.get("id", "err")}'
        )

    force_ban(
        gate_sym,
        bx_ticker,
        reason
    )

    with position_lock:
        ACTIVE_POSITIONS.pop(
            bx_ticker,
            None
        )


def track_intermediate_tp(
    gate_symbol: str,
    side: str,
    qty: int,
    inter_tp: float
):
    bx_ticker = to_bingx(
        gate_symbol
    )

    tg_log(
        bx_ticker,
        f'Трекинг inter TP={inter_tp:.6f} '
        f'сторона={side}'
    )

    for _ in range(6000):
        size = get_position_size(
            gate_symbol,
            side
        )

        if size <= 0:
            return

        current = get_current_price(
            gate_symbol
        )

        reached = (
            side == 'long'
            and current >= inter_tp
        ) or (
            side == 'short'
            and current <= inter_tp
        )

        if reached:
            tg_log(
                bx_ticker,
                f'Intermediate TP hit @ {current:.6f}',
                send_now=True
            )

            half = max(
                1,
                int(size) // 2
            )

            close_position_market(
                gate_symbol,
                side,
                half
            )

            cancel_all_price_orders(
                gate_symbol
            )

            time.sleep(0.5)

            new_sl = (
                current * (1 - INTER_TP_SL_PCT)
                if side == 'long'
                else current * (1 + INTER_TP_SL_PCT)
            )

            remaining = max(
                1,
                int(
                    get_position_size(
                        gate_symbol,
                        side
                    )
                )
            )

            sl_order = place_price_order(
                gate_symbol,
                side,
                new_sl,
                remaining,
                'sl'
            )

            if sl_order.get('id'):
                with position_lock:
                    if bx_ticker in ACTIVE_POSITIONS:
                        ACTIVE_POSITIONS[bx_ticker]['stop_loss'] = (
                            sl_order['id']
                        )

                        ACTIVE_POSITIONS[bx_ticker]['take_profit'] = None
                        ACTIVE_POSITIONS[bx_ticker]['intermediate_tp'] = None

                tg_log(
                    bx_ticker,
                    f'Новый SL @ {new_sl:.6f} '
                    f'id={sl_order["id"]}',
                    send_now=True
                )

            else:
                tg_log(
                    bx_ticker,
                    'Ошибка нового SL после inter TP',
                    send_now=True
                )

                close_position_market(
                    gate_symbol,
                    side,
                    remaining
                )

            return

        time.sleep(0.5)


def place_tpsl(
    gate_symbol: str,
    side: str,
    qty: int,
    tp: float,
    sl: float,
    inter_tp: float,
    last_price: float,
    percentage: float
):
    placing_key = (
        f'{gate_symbol}:{side}'
    )

    bx_ticker = to_bingx(
        gate_symbol
    )

    for _ in range(20):
        if placing_key not in TPSL_PLACING:
            break

        time.sleep(0.5)

    else:
        real_qty = int(
            get_position_size(
                gate_symbol,
                side
            )
        )

        tg_log(
            bx_ticker,
            f'TP/SL уже слишком долго '
            f'выставляются для {side}. '
            f'Закрываем позицию',
            send_now=True
        )

        if real_qty > 0:
            close_position_market(
                gate_symbol,
                side,
                real_qty
            )

        return

    TPSL_PLACING.add(
        placing_key
    )

    try:
        real_qty = int(
            get_position_size(
                gate_symbol,
                side
            )
        )

        if real_qty <= 0:
            tg_log(
                bx_ticker,
                'place_tpsl: no position found',
                send_now=True
            )

            return

        cancel_all_price_orders(
            gate_symbol
        )

        time.sleep(0.8)

        results = {}

        for name, trigger, order_type in (
            ('tp', tp, 'tp'),
            ('sl', sl, 'sl')
        ):
            last_result = {}

            for attempt in range(5):
                last_result = place_price_order(
                    gate_symbol,
                    side,
                    trigger,
                    real_qty,
                    order_type
                )

                results[name] = last_result

                if (
                    isinstance(last_result, dict)
                    and last_result.get('id')
                ):
                    break

                log.warning(
                    '[%s] %s place attempt %d failed: %s',
                    gate_symbol,
                    name.upper(),
                    attempt + 1,
                    last_result
                )

                time.sleep(1)

        if inter_tp:
            half = max(
                1,
                real_qty // 2
            )

            last_result = {}

            for attempt in range(3):
                last_result = place_price_order(
                    gate_symbol,
                    side,
                    inter_tp,
                    half,
                    'tp'
                )

                results['itp'] = last_result

                if (
                    isinstance(last_result, dict)
                    and last_result.get('id')
                ):
                    break

                log.warning(
                    '[%s] ITP place attempt %d failed: %s',
                    gate_symbol,
                    attempt + 1,
                    last_result
                )

                time.sleep(1)

        tp_id = (
            results.get('tp', {}).get('id')
            if isinstance(
                results.get('tp'),
                dict
            )
            else None
        )

        sl_id = (
            results.get('sl', {}).get('id')
            if isinstance(
                results.get('sl'),
                dict
            )
            else None
        )

        itp_id = (
            results.get('itp', {}).get('id')
            if isinstance(
                results.get('itp'),
                dict
            )
            else None
        )

        tg_log(
            bx_ticker,
            f'TP={tp:.6f} id={tp_id} | '
            f'SL={sl:.6f} id={sl_id} | '
            f'ITP={inter_tp or 0:.6f} id={itp_id} | '
            f'real_qty={real_qty}',
            send_now=True
        )

        if not tp_id or not sl_id:
            tg_log(
                bx_ticker,
                f'TP/SL не выставлены полностью. '
                f'tp_resp={results.get("tp")} '
                f'sl_resp={results.get("sl")} '
                f'-> закрываем позицию market',
                send_now=True
            )

            real_qty = int(
                get_position_size(
                    gate_symbol,
                    side
                )
            )

            if real_qty > 0:
                close_position_market(
                    gate_symbol,
                    side,
                    real_qty
                )

            with position_lock:
                if (
                    bx_ticker in ACTIVE_POSITIONS
                    and ACTIVE_POSITIONS[bx_ticker].get(
                        'positionSide'
                    ) == side
                ):
                    ACTIVE_POSITIONS.pop(
                        bx_ticker,
                        None
                    )

            return

        with position_lock:
            if bx_ticker in ACTIVE_POSITIONS:
                ACTIVE_POSITIONS[bx_ticker]['take_profit'] = tp_id
                ACTIVE_POSITIONS[bx_ticker]['stop_loss'] = sl_id
                ACTIVE_POSITIONS[bx_ticker]['intermediate_tp'] = itp_id

        if inter_tp and itp_id:
            threading.Thread(
                target=track_intermediate_tp,
                args=(
                    gate_symbol,
                    side,
                    real_qty,
                    inter_tp
                ),
                daemon=True
            ).start()

    except Exception as e:
        tg_log(
            bx_ticker,
            f'place_tpsl error: {e}',
            send_now=True
        )

        traceback.print_exc()

    finally:
        TPSL_PLACING.discard(
            placing_key
        )


def _calc_tp_sl(
    side: str,
    fair_price: float,
    avg_price: float
):
    if side == 'long':
        tp = (
            fair_price
            * (1 - TP_BUFFER_PCT)
        )

        sl = (
            avg_price
            * (1 - SL_PCT)
        )

        if tp <= avg_price:
            tp = (
                avg_price
                * (
                    1
                    + max(
                        TP_BUFFER_PCT,
                        0.003
                    )
                )
            )

    else:
        tp = (
            fair_price
            * (1 + TP_BUFFER_PCT)
        )

        sl = (
            avg_price
            * (1 + SL_PCT)
        )

        if tp >= avg_price:
            tp = (
                avg_price
                * (
                    1
                    - max(
                        TP_BUFFER_PCT,
                        0.003
                    )
                )
            )

    return (
        float(f'{tp:.6f}'),
        float(f'{sl:.6f}')
    )


def open_position(
    ticker: str,
    side: str,
    fair_price: float,
    last_price: float,
    percentage: float,
    current_price: float
):
    if ticker in OPENING_IN_PROGRESS:
        return

    OPENING_IN_PROGRESS.add(
        ticker
    )

    gate_sym = to_gate(
        ticker
    )

    bx_ticker = ticker

    try:
        if gate_sym in global_banlist:
            tg_log(
                bx_ticker,
                f'В GLOBAL BANLIST ({gate_sym}), пропуск',
                send_now=True
            )

            return

        with position_lock:
            existing = ACTIVE_POSITIONS.get(
                bx_ticker
            )

            open_count = len(
                ACTIVE_POSITIONS
            )

        if (
            not existing
            and open_count >= MAX_OPEN_POSITIONS
        ):
            tg_log(
                bx_ticker,
                f'Max positions ({MAX_OPEN_POSITIONS}) достигнут',
                send_now=True
            )

            return

        tp_raw = (
            fair_price * (1 - TP_BUFFER_PCT)
            if side == 'long'
            else fair_price * (1 + TP_BUFFER_PCT)
        )

        if (
            side == 'long'
            and current_price >= tp_raw
        ):
            tg_log(
                bx_ticker,
                f'Цена {current_price} уже выше TP '
                f'{tp_raw:.6f}, пропуск',
                send_now=True
            )

            return

        if (
            side == 'short'
            and current_price <= tp_raw
        ):
            tg_log(
                bx_ticker,
                f'Цена {current_price} уже ниже TP '
                f'{tp_raw:.6f}, пропуск',
                send_now=True
            )

            return

        if (
            side == 'long'
            and last_price >= fair_price
        ):
            tg_log(
                bx_ticker,
                'Условие входа не прошло '
                '(long but last >= fair)',
                send_now=True
            )

            return

        if (
            side == 'short'
            and last_price <= fair_price
        ):
            tg_log(
                bx_ticker,
                'Условие входа не прошло '
                '(short but last <= fair)',
                send_now=True
            )

            return

        qty_price = (
            current_price
            if current_price > 0
            else last_price
        )

        lev_resp = set_leverage(
            gate_sym,
            LEVERAGE
        )

        tg_log(
            bx_ticker,
            f'Set leverage x{LEVERAGE}: {lev_resp}'
        )

        if (
            isinstance(lev_resp, dict)
            and lev_resp.get('label')
        ):
            tg_log(
                bx_ticker,
                f'Не удалось установить плечо: {lev_resp}',
                send_now=True
            )

            return

        qty_price = (
            get_current_price(gate_sym)
            or qty_price
        )

        qty_contracts = contracts_from_usdt(
            gate_sym,
            POSITION_USDT,
            qty_price,
            LEVERAGE
        )

        if qty_contracts <= 0:
            available_margin = get_available_margin()
            min_qty = get_order_size_min(
                gate_sym
            )

            one_min_margin = (
                estimate_margin_for_order(
                    gate_sym,
                    min_qty,
                    qty_price,
                    LEVERAGE
                )
            )

            tg_log(
                bx_ticker,
                f'Недостаточно маржи для открытия: '
                f'available={available_margin:.6f}, '
                f'min_qty={min_qty}, '
                f'min_est_margin={one_min_margin:.6f}, '
                f'price={qty_price:.8f}, '
                f'lev={LEVERAGE}',
                send_now=True
            )

            return

        available_margin = get_available_margin()

        required_margin = (
            estimate_margin_for_order(
                gate_sym,
                qty_contracts,
                qty_price,
                LEVERAGE
            )
        )

        log.info(
            '[%s] Pre-open margin check: '
            'qty=%s available=%.8f '
            'required_est=%.8f price=%.8f lev=%s',
            gate_sym,
            qty_contracts,
            available_margin,
            required_margin,
            qty_price,
            LEVERAGE
        )

        if existing:
            existing_side = existing.get(
                'positionSide'
            )

            signal_count = existing.get(
                'signal_count',
                0
            )

            was_reversed = existing.get(
                'reversed',
                False
            )

            if existing_side == side:
                real_size = get_position_size(
                    gate_sym,
                    side
                )

                if real_size <= 0:
                    cleanup_stale_position(
                        bx_ticker,
                        reason=(
                            'position missing on exchange '
                            'during same-side signal'
                        )
                    )

                    return

                if signal_count == 0:
                    tg_log(
                        bx_ticker,
                        f'Повторный сигнал '
                        f'#{signal_count + 1} '
                        f'-> обновляем TP/SL',
                        send_now=True
                    )

                    cancel_all_price_orders(
                        gate_sym
                    )

                    with position_lock:
                        ACTIVE_POSITIONS[bx_ticker][
                            'signal_count'
                        ] = 1

                    avg_price = (
                        get_avg_price(
                            gate_sym,
                            side,
                            retries=4
                        )
                        or qty_price
                    )

                    tp, sl = _calc_tp_sl(
                        side,
                        fair_price,
                        avg_price
                    )

                    inter_tp = 0.0

                    if (
                        percentage is not None
                        and abs(percentage)
                        >= INTER_TP_THRESHOLD
                    ):
                        inter_tp = (
                            avg_price
                            + (
                                tp - avg_price
                            ) / 2
                            if side == 'long'
                            else avg_price
                            - (
                                avg_price - tp
                            ) / 2
                        )

                        inter_tp = float(
                            f'{inter_tp:.6f}'
                        )

                    place_tpsl(
                        gate_sym,
                        side,
                        int(real_size),
                        tp,
                        sl,
                        inter_tp,
                        qty_price,
                        percentage or 0.0
                    )

                    return

                threading.Thread(
                    target=force_close_and_ban,
                    args=(
                        gate_sym,
                        bx_ticker,
                        side,
                        f'спам сигналов #{signal_count + 1}'
                    ),
                    daemon=True
                ).start()

                return

            if was_reversed:
                threading.Thread(
                    target=force_close_and_ban,
                    args=(
                        gate_sym,
                        bx_ticker,
                        existing_side,
                        'повторный разворот'
                    ),
                    daemon=True
                ).start()

                return

            old_side = existing_side

            old_qty = int(
                get_position_size(
                    gate_sym,
                    old_side
                )
            )

            if old_qty <= 0:
                cleanup_stale_position(
                    bx_ticker,
                    reason=(
                        f'position missing on exchange '
                        f'during reversal to {side}'
                    )
                )

                return

            tg_log(
                bx_ticker,
                f'Разворот {old_side}->{side}, '
                f'закрываем {old_side} '
                f'({old_qty} контрактов)',
                send_now=True
            )

            cancel_all_price_orders(
                gate_sym
            )

            closed = close_position_market(
                gate_sym,
                old_side,
                old_qty
            )

            if not (
                closed.get('id')
                or closed.get('status') == 'finished'
            ):
                tg_log(
                    bx_ticker,
                    f'Не удалось закрыть старую '
                    f'позицию: {closed}',
                    send_now=True
                )

                return

            old_open_time = existing.get(
                'open_time',
                0.0
            )

            old_still_open = True

            for _ in range(10):
                time.sleep(0.5)

                if (
                    get_position_size(
                        gate_sym,
                        old_side
                    ) <= 0
                ):
                    old_still_open = False
                    break

            if old_still_open:
                tg_log(
                    bx_ticker,
                    f'Старая {old_side} позиция '
                    f'не закрылась полностью, '
                    f'новый {side} не открываем',
                    send_now=True
                )

                return

            with position_lock:
                ACTIVE_POSITIONS.pop(
                    bx_ticker,
                    None
                )

            if old_open_time:
                schedule_pnl_check(
                    gate_sym,
                    bx_ticker,
                    old_open_time,
                    old_side
                )

            time.sleep(1)

        is_reversal = (
            existing is not None
            and existing.get('positionSide') != side
        )

        order, used_qty = (
            place_market_order_safely(
                gate_sym,
                side,
                qty_contracts,
                LEVERAGE,
                qty_price
            )
        )

        tg_log(
            bx_ticker,
            f'Market order: {order}'
        )

        if not order.get('id'):
            err = str(order)

            tg_log(
                bx_ticker,
                f'Ошибка открытия: {err}',
                send_now=True
            )

            if 'liquidat' in err.lower():
                tg_log(
                    bx_ticker,
                    'Высокий риск ликвидации, пропуск',
                    send_now=True
                )

            return

        time.sleep(1.5)

        real_qty = int(
            get_position_size(
                gate_sym,
                side
            )
        )

        if real_qty <= 0:
            tg_log(
                bx_ticker,
                'Ордер отправлен, но позиция '
                'не подтвердилась на бирже',
                send_now=True
            )

            return

        with position_lock:
            ACTIVE_POSITIONS[bx_ticker] = {
                'positionSide': side,
                'orderID': order['id'],
                'open_time': time.time(),
                'stop_loss': None,
                'take_profit': None,
                'intermediate_tp': None,
                'signal_count': 0,
                'reversed': is_reversal,
            }

        avg_price = (
            get_avg_price(
                gate_sym,
                side,
                retries=8
            )
            or qty_price
        )

        tp, sl = _calc_tp_sl(
            side,
            fair_price,
            avg_price
        )

        inter_tp = 0.0

        if (
            percentage is not None
            and abs(percentage)
            >= INTER_TP_THRESHOLD
        ):
            inter_tp = (
                avg_price
                + (
                    tp - avg_price
                ) / 2
                if side == 'long'
                else avg_price
                - (
                    avg_price - tp
                ) / 2
            )

            inter_tp = float(
                f'{inter_tp:.6f}'
            )

        tg_log(
            bx_ticker,
            f'avg={avg_price:.6f} '
            f'TP={tp} '
            f'SL={sl} '
            f'ITP={inter_tp} '
            f'req_qty={qty_contracts} '
            f'real_qty={real_qty} '
            f'used_qty={used_qty}',
            send_now=True
        )

        place_tpsl(
            gate_sym,
            side,
            real_qty,
            tp,
            sl,
            inter_tp,
            qty_price,
            percentage or 0.0
        )

        return order

    except Exception as e:
        tg_log(
            bx_ticker,
            f'open_position error: {e}',
            send_now=True
        )

        traceback.print_exc()

    finally:
        OPENING_IN_PROGRESS.discard(
            ticker
        )


def sync_positions():
    global _sync_last_alive

    fail_count = 0

    while True:
        try:
            _sync_last_alive = time.time()

            resp = gate_request(
                f'/futures/{SETTLE}/dual_comp/positions',
                timeout=12
            )

            if not isinstance(resp, list):
                time.sleep(1)
                continue

            fail_count = 0

            exchange_open = set()

            for pos in resp:
                try:
                    if float(
                        pos.get('size', 0)
                    ) != 0:
                        contract = pos['contract']

                        mode = str(
                            pos.get('mode', '')
                        ).lower()

                        side = (
                            'long'
                            if 'long' in mode
                            else 'short'
                        )

                        exchange_open.add(
                            (contract, side)
                        )

                except Exception as e:
                    log.warning(
                        'sync_positions bad item %s (%s)',
                        pos,
                        e
                    )

            to_remove = []

            with position_lock:
                active_snapshot = list(
                    ACTIVE_POSITIONS.items()
                )

            for ticker, data in active_snapshot:
                gate_sym = to_gate(
                    ticker
                )

                side = data['positionSide']

                direct_size = get_position_size(
                    gate_sym,
                    side
                )

                if direct_size <= 0:
                    to_remove.append(
                        ticker
                    )

            for ticker in to_remove:
                cleanup_stale_position(
                    ticker,
                    reason=(
                        'sync direct check detected '
                        'closed position'
                    )
                )

            time.sleep(1)

        except Exception as e:
            fail_count += 1

            wait = min(
                30,
                2 ** fail_count
            )

            log.error(
                'sync_positions error attempt=%d '
                'wait=%s: %s\n%s',
                fail_count,
                wait,
                e,
                traceback.format_exc()
            )

            time.sleep(wait)


def start_sync_thread():
    global _sync_thread

    t = threading.Thread(
        target=sync_positions,
        daemon=True,
        name='sync_positions'
    )

    t.start()

    _sync_thread = t

    return t


def watchdog():
    while True:
        time.sleep(10)

        try:
            age = (
                time.time()
                - _sync_last_alive
            )

            if (
                _sync_thread is None
                or not _sync_thread.is_alive()
            ):
                start_sync_thread()

            elif age > 30:
                start_sync_thread()

        except Exception as e:
            log.error(
                'WATCHDOG error: %s',
                e
            )


def build_name_cache():
    resp = gate_request(
        f'/futures/{SETTLE}/contracts',
        params={'limit': 1000}
    )

    if not isinstance(resp, list):
        log.warning(
            'build_name_cache failed'
        )

        return

    for c in resp:
        name = c.get(
            'name',
            ''
        )

        contract = c.get(
            'contract',
            ''
        )

        if name and contract:
            _name_to_symbol_cache[name] = contract

            _name_to_symbol_cache[
                contract.replace('_USDT', '')
            ] = contract

    log.info(
        'Name cache built: %d entries',
        len(_name_to_symbol_cache)
    )


def resolve_ticker(raw: str):
    if raw.isascii():
        return raw.upper() + '_USDT'

    if not _name_to_symbol_cache:
        build_name_cache()

    contract = _name_to_symbol_cache.get(
        raw
    )

    if contract:
        return contract

    return None


_RE_TICKER = re.compile(
    r'\$`?([^`\s\n$]+)`?',
    re.IGNORECASE
)

_RE_LAST = re.compile(
    r'Last:\s*\$?\s*([0-9]*\.?[0-9]+)',
    re.IGNORECASE
)

_RE_FAIR = re.compile(
    r'Fair:\s*\$?\s*([0-9]*\.?[0-9]+)',
    re.IGNORECASE
)

_RE_PERC = re.compile(
    r'([0-9]{1,3})\s*sec:\s*'
    r'([+-]?[0-9]*\.?[0-9]+)%',
    re.IGNORECASE
)

_RE_VOL = re.compile(
    r'24h\s*Vol:\s*\$?\s*'
    r'([0-9]*\.?[0-9]+)\s*([kKmM]?)',
    re.IGNORECASE
)


def parse_volume_to_number(
    raw: str,
    suffix: str
) -> float:
    value = float(raw)

    suffix = (
        suffix or ''
    ).strip().lower()

    if suffix == 'k':
        value *= 1000

    elif suffix == 'm':
        value *= 1000000

    return value


def parse_signal(text: str):
    ticker_m = _RE_TICKER.search(text)
    last_m = _RE_LAST.search(text)
    fair_m = _RE_FAIR.search(text)

    perc_matches = {
        int(sec): float(val)
        for sec, val in _RE_PERC.findall(text)
    }

    vol_m = _RE_VOL.search(text)

    if (
        not ticker_m
        or not last_m
        or not fair_m
        or not vol_m
    ):
        return None

    raw_ticker = (
        ticker_m.group(1)
        .strip()
        .upper()
    )

    ticker = (
        raw_ticker
        if '-' in raw_ticker
        else f'{raw_ticker}-USDT'
    )

    last_price = float(
        last_m.group(1)
    )

    fair_price = float(
        fair_m.group(1)
    )

    percentage = perc_matches.get(
        60
    )

    volume_24h = parse_volume_to_number(
        vol_m.group(1),
        vol_m.group(2)
    )

    side = (
        'short'
        if last_price > fair_price
        else 'long'
    )

    return {
        'ticker': ticker,
        'side': side,
        'last_price': last_price,
        'fair_price': fair_price,
        'percentage': percentage,
        'volume_24h': volume_24h,
    }


@bot.on(
    events.NewMessage(
        chats=('gate_8p',)
    )
)
async def on_signal(event):
    try:
        text = event.raw_text or ''

        parsed = parse_signal(
            text
        )

        if not parsed:
            return

        ticker = parsed['ticker']
        last_price = parsed['last_price']
        fair_price = parsed['fair_price']
        percentage = parsed['percentage']
        side = parsed['side']
        volume_24h = parsed['volume_24h']

        if volume_24h >= 100000:
            return

        gate_sym = to_gate(
            ticker
        )

        if gate_sym in global_banlist:
            tg_log(
                ticker,
                f'{gate_sym} в global banlist, пропуск'
            )

            return

        now = time.time()

        spam_key = (
            f'{ticker}:{side}'
        )

        last_ts = _last_signal_ts.get(
            spam_key,
            0
        )

        if (
            now - last_ts
            < SPAM_COOLDOWN_SEC
        ):
            tg_log(
                ticker,
                f'SPAM GUARD: повтор сигнала '
                f'раньше {SPAM_COOLDOWN_SEC}s, пропуск'
            )

            return

        _last_signal_ts[spam_key] = now

        msg_dt = event.message.date

        if msg_dt:
            age_sec = (
                datetime.utcnow()
                .replace(tzinfo=msg_dt.tzinfo)
                - msg_dt
            ).total_seconds()

            if age_sec > SIGNAL_MAX_AGE_SEC:
                tg_log(
                    ticker,
                    f'Сигнал устарел '
                    f'({age_sec:.0f}s), пропуск'
                )

                return

        current_price = get_current_price(
            gate_sym
        )

        log.info(
            'SIGNAL: %s %s | last=%.6f '
            'fair=%.6f pct=%s vol24h=%.2f '
            'cur=%.6f',
            ticker,
            side.upper(),
            last_price,
            fair_price,
            percentage,
            volume_24h,
            current_price
        )

        threading.Thread(
            target=open_position,
            args=(
                ticker,
                side,
                fair_price,
                last_price,
                percentage or 0.0,
                current_price
            ),
            daemon=True
        ).start()

    except Exception as e:
        log.error(
            'Handler error: %s\n%s',
            e,
            traceback.format_exc()
        )


@bot.on(
    events.NewMessage(
        pattern=r'^/bangate'
    )
)
async def ban_cmd(event):
    parts = (
        event.message.message
        .strip()
        .split()
    )

    if (
        len(parts) == 1
        or parts[1] == 'help'
    ):
        await event.reply(
            'Команды банлиста:\n'
            '/bangate gatelist\n'
            '/bangate gateadd BTC_USDT\n'
            '/bangate gatedel BTC_USDT\n'
            '/bangate gateclear\n'
            '/bangate gatelocal'
        )

    elif parts[1] == 'gatelist':
        msg = (
            'Global banlist:\n'
            + '\n'.join(
                sorted(global_banlist)
            )
            if global_banlist
            else 'Global banlist пустой'
        )

        await event.reply(msg)

    elif parts[1] == 'gatelocal':
        msg = (
            'Local loss count:\n'
            + '\n'.join(
                f'{k}: {v}'
                for k, v in local_banlist.items()
            )
            if local_banlist
            else 'Local banlist пустой'
        )

        await event.reply(msg)

    elif (
        parts[1] == 'gateadd'
        and len(parts) >= 3
    ):
        sym = to_gate(
            parts[2].upper()
        )

        global_banlist.add(
            sym
        )

        save_banlist()

        await event.reply(
            f'{sym} добавлен в global banlist'
        )

    elif (
        parts[1] == 'gatedel'
        and len(parts) >= 3
    ):
        sym = to_gate(
            parts[2].upper()
        )

        if sym in global_banlist:
            global_banlist.discard(
                sym
            )

            save_banlist()

            await event.reply(
                f'{sym} удалён в global banlist'
            )

        else:
            await event.reply(
                f'{sym} не найден в banlist'
            )

    elif parts[1] == 'gateclear':
        global_banlist.clear()

        save_banlist()

        await event.reply(
            'Banlist очищен'
        )


@bot.on(
    events.NewMessage(
        pattern=r'^/gatestatus'
    )
)
async def status_cmd(event):
    with position_lock:
        lines = [
            f'Открытых позиций: '
            f'{len(ACTIVE_POSITIONS)}'
        ]

        for ticker, data in ACTIVE_POSITIONS.items():
            sc = data.get(
                'signal_count',
                0
            )

            rev = (
                'REVERSED'
                if data.get('reversed')
                else ''
            )

            lines.append(
                f'{ticker} '
                f'{data["positionSide"].upper()} '
                f'{rev} '
                f'signals={sc} '
                f'TP={data.get("take_profit")} '
                f'SL={data.get("stop_loss")}'
            )

    await event.reply(
        '\n'.join(lines)
        or 'Нет открытых позиций'
    )


@bot.on(
    events.NewMessage(
        pattern=r'^/(gatestop|gatestart|gaterestart)$'
    )
)
async def pm2_control_handler(event):
    import asyncio

    command = (
        event.message.message
        .strip()[1:]
    )

    pm2_cmds = {
        'gatestop': (
            'stop',
            'Бот остановлен'
        ),
        'gatestart': (
            'start',
            'Бот запущен'
        ),
        'gaterestart': (
            'restart',
            'Бот перезапущен'
        ),
    }

    pm2_action, label = pm2_cmds[
        command
    ]

    await event.reply(
        f'{label}...'
    )

    await asyncio.sleep(1)

    result = subprocess.run(
        [
            'pm2',
            pm2_action,
            PM2_APP_NAME
        ],
        capture_output=True,
        text=True,
        timeout=15
    )

    output = (
        result.stdout
        or result.stderr
        or '—'
    ).strip()[:500]

    if command != 'gatestop':
        await event.reply(
            f'<pre>{output}</pre>',
            parse_mode='html'
        )


if __name__ == '__main__':
    log.info(
        '=== Gate.io Trading Bot START ==='
    )

    log.info(
        'Global banlist: %s',
        global_banlist
    )

    log.info(
        'Local banlist: %s',
        local_banlist
    )

    build_name_cache()

    start_sync_thread()

    threading.Thread(
        target=watchdog,
        daemon=True,
        name='watchdog'
    ).start()

    threading.Thread(
        target=pnl_pending_worker,
        daemon=True,
        name='pnl_pending_worker'
    ).start()

    bot.start(
        phone=TG_PHONE
    )

    log.info(
        'Telegram connected, waiting for signals...'
    )

    bot.run_until_disconnected()
