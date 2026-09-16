from telethon import TelegramClient, events
import socks
import re
import urllib.parse
import requests
import time
import hmac
import hashlib
import os
import threading
import concurrent.futures
import traceback
import argparse
import json
from datetime import datetime, timedelta

from dotenv import load_dotenv


load_dotenv()


parser = argparse.ArgumentParser()
parser.add_argument('--usdt', type=float)
parser.add_argument('--leverage', type=int)
args = parser.parse_args()


usdt = args.usdt if args.usdt is not None else float(
    os.getenv("POSITION_USDT", "5")
)

leverage = args.leverage if args.leverage is not None else int(
    os.getenv("LEVERAGE", "5")
)


API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("PHONE")
TELEGRAM_SESSION = os.getenv(
    "TELEGRAM_SESSION",
    "Trading_Bot_Formats"
)

PROXY_HOST = os.getenv("PROXY_HOST")
PROXY_PORT = int(os.getenv("PROXY_PORT", "0"))
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")

proxy = (
    socks.HTTP,
    PROXY_HOST,
    PROXY_PORT,
    False,
    PROXY_USERNAME,
    PROXY_PASSWORD
)

bot = TelegramClient(
    TELEGRAM_SESSION,
    API_ID,
    API_HASH,
    proxy=proxy
)


API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BASE_URL = os.getenv(
    "BINGX_BASE_URL",
    "https://open-api.bingx.com"
)


API_LOG = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_LOG_CHAT_ID = os.getenv("TELEGRAM_LOG_CHAT_ID")

PROXY_URL = (
    f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}"
    f"@{PROXY_HOST}:{PROXY_PORT}"
)

proxies = {
    "http": PROXY_URL,
    "https": PROXY_URL,
}


SIGNAL_CHAT = os.getenv(
    "SIGNAL_CHAT",
    "Digash_Formations"
)


list_logs = {}
ACTIVE_POSITIONS = {}
local_banlist = {}
all_signals = []
white_list = set()

leverage_default = 10
usdt_amount_default = 10

HEDGE_TRIGGER_PERCENT = 0.01
HEDGE_RECOVERY_PERCENT = 0.0005
HEDGE_RECOVERY_CONFIRMATIONS = 2

HEDGE_MIN_HOLD_SECONDS = 3
HEDGE_CHECK_INTERVAL = 0.2
HEDGE_QTY_MULTIPLIER = 1.5

HEDGE_TRAILING_CALLBACK_PERCENT = 0
HEDGE_MIN_PROFIT_TO_TRAIL = 999

HEDGE_CLOSE_NEAR_HEDGE_ENTRY_PERCENT = 0.001
HEDGE_REOPEN_COOLDOWN_SECONDS = 0


BANLIST_FILE = "global_banlist.json"


def load_banlist():
    global global_banlist

    if not os.path.exists(BANLIST_FILE):
        return

    with open(BANLIST_FILE, "r") as f:
        data = json.load(f)

    saved_time = datetime.fromisoformat(data["saved_at"])

    if datetime.now() - saved_time > timedelta(days=3):
        print("BANLIST CLEARED (3 days passed)")
        global_banlist = set()
        save_banlist()
    else:
        global_banlist = set(data["symbols"])
        print("BANLIST LOADED:", global_banlist)


def save_banlist():
    with open(BANLIST_FILE, "w") as f:
        json.dump(
            {
                "saved_at": datetime.now().isoformat(),
                "symbols": list(global_banlist)
            },
            f
        )


global_banlist = set()
load_banlist()

position_lock = threading.Lock()

symbol_locks = {}
symbol_locks_guard = threading.Lock()


def get_symbol_lock(symbol):
    with symbol_locks_guard:
        if symbol not in symbol_locks:
            symbol_locks[symbol] = threading.Lock()

        return symbol_locks[symbol]


def now_ms():
    return str(int(time.time() * 1000))


def create_order_with_retries(params, retries=3, delay=0.7):
    last_response = {}

    for attempt in range(1, retries + 1):
        order_params = params.copy()
        order_params["timestamp"] = now_ms()

        response = send_request(
            "/openApi/swap/v2/trade/order",
            order_params,
            method="POST"
        )

        last_response = response

        print(
            f"CREATE ORDER ATTEMPT {attempt}/{retries}:",
            response
        )

        if response and response.get("code") == 0:
            return response

        time.sleep(delay)

    return last_response


def cancel_position_protection_orders(symbol):
    with position_lock:
        position_data = ACTIVE_POSITIONS.get(symbol, {}).copy()

    cancelled = {}

    for order_key in (
        "take_profit",
        "stop_loss",
        "intermediate_tp"
    ):
        order_id = position_data.get(order_key)

        if not order_id:
            continue

        response = cancel_order_safely(
            symbol,
            order_id
        )

        cancelled[order_key] = response

        logs_process(
            f"CANCEL PROTECTION {order_key} ({order_id}): {response}",
            symbol
        )

    return cancelled


def cancel_order_safely(symbol, order_id):
    if not order_id:
        return None

    response = send_request(
        "/openApi/swap/v2/trade/order",
        {
            "symbol": symbol,
            "orderId": str(order_id),
            "timestamp": now_ms()
        },
        method="DELETE"
    )

    print(
        f"CANCEL ORDER {symbol} {order_id}:",
        response
    )

    return response


def close_position_market(symbol, position_side):
    if position_side == "LONG":
        close_side = "SELL"
    else:
        close_side = "BUY"

    qty = get_position_size(
        symbol,
        position_side
    )

    if qty <= 0:
        print(
            "NO POSITION TO CLOSE:",
            symbol,
            position_side
        )

        return {
            "code": 101205,
            "msg": "no position"
        }

    response = create_order_with_retries(
        {
            "symbol": symbol,
            "side": close_side,
            "positionSide": position_side,
            "type": "MARKET",
            "quantity": str(round(qty, 6))
        },
        retries=3,
        delay=0.7
    )

    print(
        "CLOSE POSITION MARKET:",
        symbol,
        position_side,
        response
    )

    return response


def send_file_to_telegram(filepath):
    url = (
        f"https://api.telegram.org/bot"
        f"{API_LOG}/sendPhoto"
    )

    with open(filepath, "r", encoding="utf-8") as file:
        text = file.read()

    with open("formats.jpg", "rb") as photo:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_LOG_CHAT_ID,
                "caption": text[:1000]
            },
            files={
                "photo": photo
            },
            proxies=proxies,
            timeout=30
        )

    return response


def logs_process(message, symbol, status=False):
    if symbol not in list_logs:
        list_logs[symbol] = []

    list_logs[symbol].append(
        str(message) + "\n"
    )

    if status:
        with open(
            "logsbing17",
            "w",
            encoding="utf-8"
        ) as file:
            file.writelines(
                list_logs[symbol]
            )

        send_file_to_telegram(
            "logsbing17"
        )

        list_logs[symbol].clear()


def get_position_size(symbol, position_side):
    positions_request = send_request(
        "/openApi/swap/v2/user/positions",
        {
            "symbol": symbol,
            "timestamp": now_ms()
        },
        method="GET"
    )

    print(
        "GET POSITION SIZE RESPONSE:",
        positions_request
    )

    if (
        not positions_request
        or positions_request.get("code") != 0
    ):
        return 0

    for pos in positions_request.get(
        "data",
        []
    ):
        if (
            pos.get("symbol") == symbol
            and pos.get("positionSide") == position_side
        ):
            qty = abs(
                float(
                    pos.get(
                        "positionAmt",
                        0
                    )
                )
            )

            print(
                "POSITION SIZE FOUND:",
                symbol,
                position_side,
                qty
            )

            return qty

    print(
        "POSITION SIZE NOT FOUND:",
        symbol,
        position_side
    )

    return 0


def get_current_price(symbol):
    response = send_request(
        "/openApi/swap/v2/quote/ticker",
        {
            "symbol": symbol
        },
        method="GET"
    )

    if not response:
        print(
            "GET CURRENT PRICE ERROR: empty response",
            symbol
        )
        return None

    if response.get("code") != 0:
        print(
            "GET CURRENT PRICE API ERROR:",
            symbol,
            response
        )
        return None

    data = response.get("data")

    if not isinstance(data, dict):
        print(
            "GET CURRENT PRICE DATA ERROR:",
            symbol,
            response
        )
        return None

    last_price = data.get("lastPrice")

    if last_price is None:
        print(
            "GET CURRENT PRICE NO lastPrice:",
            symbol,
            response
        )
        return None

    try:
        return float(last_price)

    except Exception as e:
        print(
            "GET CURRENT PRICE FLOAT ERROR:",
            symbol,
            last_price,
            e
        )
        return None


def get_last_closed_pnl(symbol):
    response = send_request(
        "/openApi/swap/v2/trade/allOrders",
        {
            "symbol": symbol,
            "timestamp": now_ms()
        },
        method="GET"
    )

    orders = response.get(
        "data",
        {}
    ).get(
        "orders",
        []
    )

    if not orders:
        return 0

    closed_orders = sorted(
        orders,
        key=lambda x: int(
            x.get(
                "updateTime",
                0
            )
        ),
        reverse=True
    )

    for order in closed_orders:
        profit = float(
            order.get(
                "profit",
                0
            )
        )

        if profit != 0:
            print(
                "FOUND CLOSED PNL:",
                profit
            )

            return profit

    return 0


def sign_string(query_string):
    return hmac.new(
        API_SECRET.encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()


def send_request(path, params, method="POST"):
    try:
        headers = {
            "X-BX-APIKEY": API_KEY
        }

        params = {
            key: str(value)
            for key, value in params.items()
        }

        query_string = urllib.parse.urlencode(
            params
        )

        signature = sign_string(
            query_string
        )

        url = (
            BASE_URL
            + path
            + "?"
            + query_string
            + "&signature="
            + signature
        )

        if method == "GET":
            response = requests.get(
                url,
                headers=headers,
                timeout=10
            )

        elif method == "POST":
            response = requests.post(
                url,
                headers=headers,
                timeout=10
            )

        elif method == "DELETE":
            response = requests.delete(
                url,
                headers=headers,
                timeout=10
            )

        else:
            raise ValueError(
                f"Unsupported method: {method}"
            )

        response.raise_for_status()

        return response.json()

    except requests.exceptions.Timeout as error:
        print(
            "BINGX REQUEST TIMEOUT:",
            method,
            path,
            error
        )

        return None

    except Exception as error:
        print(
            "ERROR IN send_request:",
            method,
            path,
            error
        )

        return None


def return_pnl(order_id, symbol):
    response_pnl = send_request(
        "/openApi/swap/v2/trade/order",
        {
            "symbol": symbol,
            "orderId": order_id,
            "timestamp": now_ms()
        },
        method="GET"
    )

    try:
        order_data = response_pnl.get(
            "data",
            {}
        ).get(
            "order",
            {}
        )

        profit = float(
            order_data.get(
                "profit",
                0
            )
        )

    except Exception:
        print(
            f"FAILED TO GET PNL {symbol}"
        )
        return

    print(
        "PNL:",
        profit
    )

    is_negative = profit < 0

    sorting_banlist(
        symbol,
        is_negative
    )

    logs_process(
        symbol + " " + str(profit) + " PROFIT",
        symbol,
        True
    )


def sorting_banlist(symbol, is_negative):
    global all_signals

    if not is_negative:
        print("PNL POSITIVE")
        print(
            "WHITE LIST BEFORE:",
            white_list
        )

        white_list.add(symbol)

        all_signals = [
            x for x in all_signals
            if x != symbol
        ]

        print(
            "WHITE LIST:",
            white_list
        )

        return

    local_banlist[symbol] = (
        local_banlist.get(symbol, 0) + 1
    )

    print(
        "PNL NEGATIVE:",
        local_banlist
    )

    if local_banlist[symbol] >= 2:
        logs_process(
            symbol + " added to GLOBAL BLOCK LIST",
            symbol,
            True
        )

        print(
            symbol,
            "added to GLOBAL BLOCK LIST"
        )

        global_banlist.add(symbol)
        save_banlist()

        del local_banlist[symbol]

        white_list.discard(symbol)

    logs_process(
        "LOCAL BAN LIST "
        + str(local_banlist)
        + " GLOBAL BAN LIST "
        + str(global_banlist),
        symbol,
        True
    )


missing_counter = {}
no_pnl_counter = {}


def sync_positions():
    while True:
        try:
            response = send_request(
                "/openApi/swap/v2/user/positions",
                {
                    "timestamp": now_ms()
                },
                method="GET"
            )

            if not response:
                time.sleep(1)
                continue

            exchange_positions = response.get(
                "data",
                []
            )

            exchange_active = set()

            for pos in exchange_positions:
                if float(
                    pos.get(
                        "positionAmt",
                        0
                    )
                ) != 0:

                    symbol = pos["symbol"]
                    position_side = pos[
                        "positionSide"
                    ]

                    exchange_active.add(
                        (
                            symbol,
                            position_side
                        )
                    )

            with position_lock:
                local_copy = list(
                    ACTIVE_POSITIONS.items()
                )

                for ticker, data in local_copy:
                    local_side = data.get(
                        "positionSide"
                    )

                    if (
                        ticker,
                        local_side
                    ) not in exchange_active:

                        missing_counter[ticker] = (
                            missing_counter.get(
                                ticker,
                                0
                            ) + 1
                        )

                        print(
                            f"POSITION CHECK {ticker}: "
                            f"{missing_counter[ticker]}/5"
                        )

                        if missing_counter[ticker] < 5:
                            continue

                        print(
                            f"POSITION CONFIRMED CLOSED: "
                            f"{ticker}"
                        )

                        real_qty = get_position_size(
                            ticker,
                            local_side
                        )

                        if real_qty > 0:
                            print(
                                f"FALSE CLOSE DETECTED "
                                f"{ticker}"
                            )

                            missing_counter[ticker] = 0
                            continue

                        pnl = get_last_closed_pnl(
                            ticker
                        )

                        print(
                            f"{ticker} CLOSED PNL:",
                            pnl
                        )

                        if pnl == 0:
                            no_pnl_counter[ticker] = (
                                no_pnl_counter.get(
                                    ticker,
                                    0
                                ) + 1
                            )

                            print(
                                f"NO CLOSED PNL FOR {ticker} "
                                f"({no_pnl_counter[ticker]}/10)"
                            )

                            if no_pnl_counter[ticker] < 10:
                                continue

                            print(
                                f"FORCE REMOVING {ticker} "
                                f"FROM ACTIVE_POSITIONS"
                            )

                            logs_process(
                                "POSITION REMOVED "
                                "(PNL NOT FOUND) "
                                + ticker,
                                ticker,
                                True
                            )

                            del ACTIVE_POSITIONS[
                                ticker
                            ]

                            missing_counter.pop(
                                ticker,
                                None
                            )

                            no_pnl_counter.pop(
                                ticker,
                                None
                            )

                            continue

                        no_pnl_counter.pop(
                            ticker,
                            None
                        )

                        sorting_banlist(
                            ticker,
                            pnl < 0
                        )

                        logs_process(
                            "POSITION CONFIRMED CLOSED "
                            + ticker,
                            ticker,
                            True
                        )

                        del ACTIVE_POSITIONS[
                            ticker
                        ]

                        missing_counter.pop(
                            ticker,
                            None
                        )

                    else:
                        missing_counter[ticker] = 0

            time.sleep(1)

        except Exception as e:
            print(
                "SYNC POSITIONS ERROR:",
                e
            )


def tracking_stop_nolose(
        symbol,
        position_side,
        quantity,
        side,
        intermediate_take,
        fair_price
):
    try:
        if position_side == "LONG":
            closed_position = "SELL"
        else:
            closed_position = "BUY"

        real_qty = get_position_size(
            symbol,
            position_side
        )

        logs_process(
            symbol
            + " POSITION TRACKING STARTED",
            symbol,
            True
        )

        quantity = float(quantity)

        max_checks = 5000
        checks = 0

        while True:
            positions_request = send_request(
                "/openApi/swap/v2/user/positions",
                {
                    "symbol": symbol,
                    "timestamp": now_ms()
                },
                method="GET"
            )

            if not positions_request:
                time.sleep(0.5)
                continue

            positions = positions_request.get(
                "data",
                []
            )

            active_position = None

            for pos in positions:
                if (
                    pos["symbol"] == symbol
                    and pos["positionSide"] == position_side
                    and float(
                        pos["positionAmt"]
                    ) != 0
                ):
                    active_position = pos
                    break

            if active_position is None:
                print("POSITION CLOSED")
                break

            current_price = get_current_price(
                symbol
            )

            if current_price is None:
                time.sleep(0.5)
                continue

            reached_be = (
                (
                    position_side == "LONG"
                    and current_price >= intermediate_take
                )
                or
                (
                    position_side == "SHORT"
                    and current_price <= intermediate_take
                )
            )

            if reached_be:
                print(
                    "MOVE SL TO BREAKEVEN"
                )

                logs_process(
                    "SL MOVED",
                    symbol,
                    True
                )

                open_orders = send_request(
                    "/openApi/swap/v2/trade/openOrders",
                    {
                        "symbol": symbol,
                        "timestamp": now_ms()
                    },
                    method="GET"
                )

                orders = open_orders.get(
                    "data",
                    {}
                ).get(
                    "orders",
                    []
                )

                for order in orders:
                    if (
                        order.get("type")
                        == "STOP_MARKET"
                        and order.get("positionSide")
                        == position_side
                    ):
                        send_request(
                            "/openApi/swap/v2/trade/order",
                            {
                                "symbol": symbol,
                                "orderId": order[
                                    "orderId"
                                ],
                                "timestamp": now_ms()
                            },
                            method="DELETE"
                        )

                        print(
                            "OLD SL DELETED"
                        )

                        logs_process(
                            "OLD SL DELETED",
                            symbol,
                            True
                        )

                time.sleep(0.3)

                if position_side == "LONG":
                    new_sl_price = (
                        current_price * 0.97
                    )
                else:
                    new_sl_price = (
                        current_price * 1.03
                    )

                new_sl = send_request(
                    "/openApi/swap/v2/trade/order",
                    {
                        "symbol": symbol,
                        "side": side,
                        "positionSide": position_side,
                        "type": "STOP_MARKET",
                        "quantity": str(quantity),
                        "stopPrice": str(new_sl_price),
                        "workingType": "CONTRACT_PRICE",
                        "timestamp": now_ms()
                    }
                )

                if (
                    not new_sl
                    or new_sl.get("code") != 0
                ):
                    print(
                        "BREAKEVEN STOP LOSS ERROR"
                    )

                    closed_position_bing = send_request(
                        "/openApi/swap/v2/trade/order",
                        {
                            "symbol": symbol,
                            "side": closed_position,
                            "positionSide": position_side,
                            "type": "MARKET",
                            "quantity": str(real_qty),
                            "timestamp": now_ms()
                        }
                    )

                    print(
                        "EMERGENCY CLOSE:",
                        closed_position_bing
                    )

                    break

                print(
                    "NEW BREAKEVEN SL:",
                    new_sl
                )

                logs_process(
                    "NEW BREAKEVEN SL",
                    symbol,
                    True
                )

                break

            time.sleep(0.5)

        print(
            "TRACKING FINISHED"
        )

    except Exception as e:
        print(
            "TRACKING ERROR:",
            e
        )


def get_order_id_from_response(response):
    if (
        not response
        or response.get("code") != 0
    ):
        return None

    return (
        response
        .get("data", {})
        .get("order", {})
        .get("orderId")
    )


def cancel_hedge_orders(symbol):
    with position_lock:
        position_data = (
            ACTIVE_POSITIONS
            .get(symbol, {})
            .copy()
        )

    cancelled = {}

    for order_key in (
        "hedge_open_order",
        "hedge_exit_order"
    ):
        order_id = position_data.get(
            order_key
        )

        if not order_id:
            continue

        response = cancel_order_safely(
            symbol,
            order_id
        )

        cancelled[order_key] = response

        logs_process(
            f"CANCEL HEDGE ORDER "
            f"{order_key} ({order_id}): "
            f"{response}",
            symbol
        )

    with position_lock:
        if symbol in ACTIVE_POSITIONS:
            ACTIVE_POSITIONS[symbol][
                "hedge_open_order"
            ] = None

            ACTIVE_POSITIONS[symbol][
                "hedge_exit_order"
            ] = None

    return cancelled


def place_hedge_entry_stop(
        symbol,
        hedge_position_side,
        hedge_open_side,
        hedge_qty,
        hedge_trigger_price
):
    params = {
        "symbol": symbol,
        "side": hedge_open_side,
        "positionSide": hedge_position_side,
        "type": "STOP_MARKET",
        "quantity": str(
            round(
                float(hedge_qty),
                6
            )
        ),
        "stopPrice": str(
            round(
                float(hedge_trigger_price),
                6
            )
        ),
        "workingType": "CONTRACT_PRICE"
    }

    response = create_order_with_retries(
        params,
        retries=3,
        delay=0.3
    )

    print(
        "PLACE HEDGE ENTRY STOP:",
        symbol,
        "side=",
        hedge_open_side,
        "position_side=",
        hedge_position_side,
        "qty=",
        hedge_qty,
        "trigger=",
        hedge_trigger_price,
        "response=",
        response
    )

    logs_process(
        f"PLACE HEDGE ENTRY STOP: "
        f"side={hedge_open_side}, "
        f"position_side={hedge_position_side}, "
        f"qty={hedge_qty}, "
        f"trigger={hedge_trigger_price}, "
        f"response={response}",
        symbol,
        True
    )

    return response


def place_hedge_exit_stop(
        symbol,
        hedge_position_side,
        hedge_qty,
        hedge_trigger_price
):
    if hedge_position_side == "LONG":
        hedge_close_side = "SELL"

    elif hedge_position_side == "SHORT":
        hedge_close_side = "BUY"

    else:
        print(
            "UNKNOWN HEDGE POSITION SIDE:",
            hedge_position_side
        )

        return None

    params = {
        "symbol": symbol,
        "side": hedge_close_side,
        "positionSide": hedge_position_side,
        "type": "STOP_MARKET",
        "quantity": str(
            round(
                float(hedge_qty),
                6
            )
        ),
        "stopPrice": str(
            round(
                float(hedge_trigger_price),
                6
            )
        ),
        "workingType": "CONTRACT_PRICE"
    }

    response = create_order_with_retries(
        params,
        retries=3,
        delay=0.2
    )

    print(
        "PLACE HEDGE EXIT STOP:",
        symbol,
        "close_side=",
        hedge_close_side,
        "position_side=",
        hedge_position_side,
        "qty=",
        hedge_qty,
        "trigger=",
        hedge_trigger_price,
        "response=",
        response
    )

    logs_process(
        f"PLACE HEDGE EXIT STOP: "
        f"close_side={hedge_close_side}, "
        f"position_side={hedge_position_side}, "
        f"qty={hedge_qty}, "
        f"trigger={hedge_trigger_price}, "
        f"response={response}",
        symbol,
        True
    )

    return response


def tracking_hedge_mode(
        symbol,
        stop_loss,
        entry_price,
        entry_position_side
):
    try:
        entry_price = float(
            entry_price
        )

        stop_loss = float(
            stop_loss
        )

        if entry_position_side == "LONG":
            hedge_position_side = "SHORT"
            hedge_open_side = "SELL"

            hedge_trigger_price = (
                entry_price
                * (1 - HEDGE_TRIGGER_PERCENT)
            )

            recovery_price = (
                entry_price
                * (1 + HEDGE_RECOVERY_PERCENT)
            )

        elif entry_position_side == "SHORT":
            hedge_position_side = "LONG"
            hedge_open_side = "BUY"

            hedge_trigger_price = (
                entry_price
                * (1 + HEDGE_TRIGGER_PERCENT)
            )

            recovery_price = (
                entry_price
                * (1 - HEDGE_RECOVERY_PERCENT)
            )

        else:
            print(
                "UNKNOWN MAIN POSITION SIDE:",
                entry_position_side
            )

            return

        hedge_trigger_price = float(
            f"{hedge_trigger_price:.6f}"
        )

        recovery_price = float(
            f"{recovery_price:.6f}"
        )

        hedge_opened = False
        hedge_cycle_active = False

        hedge_open_price = None
        hedge_best_price = None

        hedge_open_order_id = None
        hedge_exit_order_id = None

        main_missing_counter = 0
        hedge_missing_counter = 0
        recovery_counter = 0

        print(
            "HEDGE TRACKING START:",
            symbol,
            "main_side=",
            entry_position_side,
            "hedge_side=",
            hedge_position_side,
            "entry=",
            entry_price,
            "trigger=",
            hedge_trigger_price,
            "recovery=",
            recovery_price,
            "stop=",
            stop_loss
        )

        logs_process(
            f"HEDGE START: "
            f"main_side={entry_position_side}, "
            f"hedge_side={hedge_position_side}, "
            f"entry={entry_price}, "
            f"trigger={hedge_trigger_price}, "
            f"recovery={recovery_price}, "
            f"stop={stop_loss}",
            symbol,
            True
        )

        while True:
            main_qty = get_position_size(
                symbol,
                entry_position_side
            )

            if main_qty <= 0:
                main_missing_counter += 1

                print(
                    "MAIN POSITION NOT FOUND:",
                    symbol,
                    entry_position_side,
                    f"{main_missing_counter}/5"
                )

                if main_missing_counter < 5:
                    time.sleep(
                        HEDGE_CHECK_INTERVAL
                    )
                    continue

                print(
                    "MAIN POSITION CONFIRMED CLOSED:",
                    symbol
                )

                cancel_result = cancel_hedge_orders(
                    symbol
                )

                hedge_qty = get_position_size(
                    symbol,
                    hedge_position_side
                )

                close_hedge_result = None

                if hedge_qty > 0:
                    close_hedge_result = (
                        close_position_market(
                            symbol,
                            hedge_position_side
                        )
                    )

                print(
                    "CLOSE HEDGE BECAUSE MAIN IS CLOSED:",
                    close_hedge_result
                )

                logs_process(
                    "MAIN CLOSED. "
                    "CANCEL HEDGE ORDERS: "
                    + str(cancel_result),
                    symbol
                )

                logs_process(
                    "MAIN CLOSED. "
                    "CLOSE HEDGE: "
                    + str(close_hedge_result),
                    symbol,
                    True
                )

                with position_lock:
                    ACTIVE_POSITIONS.pop(
                        symbol,
                        None
                    )

                return

            main_missing_counter = 0

            current_price = get_current_price(
                symbol
            )

            if current_price is None:
                time.sleep(
                    HEDGE_CHECK_INTERVAL
                )
                continue

            hedge_qty = get_position_size(
                symbol,
                hedge_position_side
            )

            stop_loss_reached = (
                (
                    entry_position_side == "LONG"
                    and current_price <= stop_loss
                )
                or
                (
                    entry_position_side == "SHORT"
                    and current_price >= stop_loss
                )
            )

            should_open_hedge = (
                (
                    entry_position_side == "LONG"
                    and current_price <= hedge_trigger_price
                )
                or
                (
                    entry_position_side == "SHORT"
                    and current_price >= hedge_trigger_price
                )
            )

            price_recovered = (
                (
                    entry_position_side == "LONG"
                    and current_price >= recovery_price
                )
                or
                (
                    entry_position_side == "SHORT"
                    and current_price <= recovery_price
                )
            )

            print(
                "HEDGE STATE:",
                symbol,
                "price=",
                current_price,
                "entry=",
                entry_price,
                "trigger=",
                hedge_trigger_price,
                "recovery=",
                recovery_price,
                "main_qty=",
                main_qty,
                "hedge_qty=",
                hedge_qty,
                "hedge_opened=",
                hedge_opened,
                "hedge_cycle_active=",
                hedge_cycle_active,
                "entry_stop_id=",
                hedge_open_order_id,
                "exit_stop_id=",
                hedge_exit_order_id,
                "should_open=",
                should_open_hedge,
                "recovered=",
                price_recovered,
                "stop_reached=",
                stop_loss_reached
            )

            if stop_loss_reached:
                print(
                    "FINAL STOP LOSS ZONE REACHED:",
                    symbol
                )

                cancel_main_protection = (
                    cancel_position_protection_orders(
                        symbol
                    )
                )

                cancel_hedge_protection = (
                    cancel_hedge_orders(
                        symbol
                    )
                )

                close_main = close_position_market(
                    symbol,
                    entry_position_side
                )

                close_hedge = close_position_market(
                    symbol,
                    hedge_position_side
                )

                logs_process(
                    "FINAL STOP CANCEL MAIN PROTECTION: "
                    + str(cancel_main_protection),
                    symbol
                )

                logs_process(
                    "FINAL STOP CANCEL HEDGE PROTECTION: "
                    + str(cancel_hedge_protection),
                    symbol
                )

                logs_process(
                    "FINAL STOP CLOSE MAIN: "
                    + str(close_main),
                    symbol
                )

                logs_process(
                    "FINAL STOP CLOSE HEDGE: "
                    + str(close_hedge),
                    symbol,
                    True
                )

                with position_lock:
                    ACTIVE_POSITIONS.pop(
                        symbol,
                        None
                    )

                return

            if hedge_cycle_active:
                if not price_recovered:
                    recovery_counter = 0

                    if hedge_qty > 0:
                        hedge_missing_counter = 0
                        hedge_opened = True

                        if hedge_exit_order_id:
                            cancel_order_safely(
                                symbol,
                                hedge_exit_order_id
                            )

                            hedge_exit_order_id = None

                            with position_lock:
                                if symbol in ACTIVE_POSITIONS:
                                    ACTIVE_POSITIONS[
                                        symbol
                                    ][
                                        "hedge_exit_order"
                                    ] = None

                        with position_lock:
                            if symbol in ACTIVE_POSITIONS:
                                ACTIVE_POSITIONS[
                                    symbol
                                ][
                                    "hedge_active"
                                ] = True

                                ACTIVE_POSITIONS[
                                    symbol
                                ][
                                    "hedge_side"
                                ] = hedge_position_side

                                ACTIVE_POSITIONS[
                                    symbol
                                ][
                                    "hedge_open_price"
                                ] = hedge_open_price

                    else:
                        hedge_missing_counter += 1

                        print(
                            "HEDGE MISSING INSIDE ZONE:",
                            symbol,
                            f"{hedge_missing_counter}/5"
                        )

                        if (
                            hedge_opened
                            and hedge_missing_counter >= 5
                        ):
                            hedge_qty_to_open = round(
                                float(main_qty)
                                * HEDGE_QTY_MULTIPLIER,
                                6
                            )

                            reopen_response = (
                                create_order_with_retries(
                                    {
                                        "symbol": symbol,
                                        "side": hedge_open_side,
                                        "positionSide": hedge_position_side,
                                        "type": "MARKET",
                                        "quantity": str(
                                            hedge_qty_to_open
                                        )
                                    },
                                    retries=3,
                                    delay=0.2
                                )
                            )

                            print(
                                "REOPEN HEDGE:",
                                reopen_response
                            )

                            logs_process(
                                "REOPEN HEDGE: "
                                + str(reopen_response),
                                symbol,
                                True
                            )

                            if (
                                reopen_response
                                and reopen_response.get(
                                    "code"
                                ) == 0
                            ):
                                hedge_missing_counter = 0
                                hedge_opened = True

                    time.sleep(
                        HEDGE_CHECK_INTERVAL
                    )

                    continue

                recovery_counter += 1

                print(
                    "HEDGE RECOVERY CONFIRMATION:",
                    symbol,
                    f"{recovery_counter}/"
                    f"{HEDGE_RECOVERY_CONFIRMATIONS}"
                )

                if (
                    recovery_counter
                    < HEDGE_RECOVERY_CONFIRMATIONS
                ):
                    time.sleep(
                        HEDGE_CHECK_INTERVAL
                    )
                    continue

                if hedge_open_order_id:
                    cancel_order_safely(
                        symbol,
                        hedge_open_order_id
                    )

                    hedge_open_order_id = None

                hedge_qty = get_position_size(
                    symbol,
                    hedge_position_side
                )

                if hedge_qty > 0:
                    close_hedge_response = (
                        close_position_market(
                            symbol,
                            hedge_position_side
                        )
                    )

                    print(
                        "CLOSE HEDGE AFTER ZONE RECOVERY:",
                        close_hedge_response
                    )

                    logs_process(
                        "CLOSE HEDGE AFTER ZONE RECOVERY: "
                        + str(close_hedge_response),
                        symbol,
                        True
                    )

                    if not (
                        close_hedge_response
                        and close_hedge_response.get(
                            "code"
                        ) == 0
                    ):
                        time.sleep(
                            HEDGE_CHECK_INTERVAL
                        )
                        continue

                hedge_cycle_active = False
                hedge_opened = False
                hedge_open_price = None
                hedge_best_price = None
                hedge_open_order_id = None
                hedge_exit_order_id = None
                hedge_missing_counter = 0
                recovery_counter = 0

                with position_lock:
                    if symbol in ACTIVE_POSITIONS:
                        ACTIVE_POSITIONS[
                            symbol
                        ][
                            "hedge_active"
                        ] = False

                        ACTIVE_POSITIONS[
                            symbol
                        ][
                            "hedge_side"
                        ] = None

                        ACTIVE_POSITIONS[
                            symbol
                        ][
                            "hedge_open_price"
                        ] = None

                        ACTIVE_POSITIONS[
                            symbol
                        ][
                            "hedge_open_order"
                        ] = None

                        ACTIVE_POSITIONS[
                            symbol
                        ][
                            "hedge_exit_order"
                        ] = None

                logs_process(
                    "HEDGE CYCLE FINISHED",
                    symbol,
                    True
                )

                time.sleep(
                    HEDGE_CHECK_INTERVAL
                )

                continue

            if hedge_qty > 0:
                hedge_cycle_active = True
                hedge_opened = True
                hedge_missing_counter = 0
                hedge_open_order_id = None

                if hedge_open_price is None:
                    hedge_open_price = current_price

                hedge_best_price = current_price

                with position_lock:
                    if symbol in ACTIVE_POSITIONS:
                        ACTIVE_POSITIONS[
                            symbol
                        ][
                            "hedge_active"
                        ] = True

                        ACTIVE_POSITIONS[
                            symbol
                        ][
                            "hedge_side"
                        ] = hedge_position_side

                        ACTIVE_POSITIONS[
                            symbol
                        ][
                            "hedge_open_price"
                        ] = hedge_open_price

                        ACTIVE_POSITIONS[
                            symbol
                        ][
                            "hedge_open_order"
                        ] = None

                        ACTIVE_POSITIONS[
                            symbol
                        ][
                            "hedge_exit_order"
                        ] = None

                logs_process(
                    f"HEDGE DETECTED: "
                    f"side={hedge_position_side}, "
                    f"qty={hedge_qty}, "
                    f"price={hedge_open_price}",
                    symbol,
                    True
                )

                time.sleep(
                    HEDGE_CHECK_INTERVAL
                )

                continue

            if should_open_hedge:
                hedge_qty_to_open = round(
                    float(main_qty)
                    * HEDGE_QTY_MULTIPLIER,
                    6
                )

                open_hedge_response = (
                    create_order_with_retries(
                        {
                            "symbol": symbol,
                            "side": hedge_open_side,
                            "positionSide": hedge_position_side,
                            "type": "MARKET",
                            "quantity": str(
                                hedge_qty_to_open
                            )
                        },
                        retries=3,
                        delay=0.2
                    )
                )

                print(
                    "OPEN HEDGE MARKET:",
                    open_hedge_response
                )

                logs_process(
                    "OPEN HEDGE MARKET: "
                    + str(open_hedge_response),
                    symbol,
                    True
                )

                if (
                    open_hedge_response
                    and open_hedge_response.get(
                        "code"
                    ) == 0
                ):
                    hedge_cycle_active = True
                    hedge_opened = True
                    hedge_open_price = current_price
                    hedge_best_price = current_price
                    hedge_missing_counter = 0

                time.sleep(
                    HEDGE_CHECK_INTERVAL
                )

                continue

            if hedge_open_order_id is None:
                hedge_qty_to_open = round(
                    float(main_qty)
                    * HEDGE_QTY_MULTIPLIER,
                    6
                )

                entry_stop_response = (
                    place_hedge_entry_stop(
                        symbol,
                        hedge_position_side,
                        hedge_open_side,
                        hedge_qty_to_open,
                        hedge_trigger_price
                    )
                )

                hedge_open_order_id = (
                    get_order_id_from_response(
                        entry_stop_response
                    )
                )

                if hedge_open_order_id:
                    with position_lock:
                        if symbol in ACTIVE_POSITIONS:
                            ACTIVE_POSITIONS[
                                symbol
                            ][
                                "hedge_open_order"
                            ] = hedge_open_order_id

                            ACTIVE_POSITIONS[
                                symbol
                            ][
                                "hedge_active"
                            ] = False

                            ACTIVE_POSITIONS[
                                symbol
                            ][
                                "hedge_side"
                            ] = hedge_position_side

                    logs_process(
                        f"HEDGE ENTRY STOP INSTALLED: "
                        f"id={hedge_open_order_id}, "
                        f"trigger={hedge_trigger_price}, "
                        f"qty={hedge_qty_to_open}",
                        symbol,
                        True
                    )

            time.sleep(
                HEDGE_CHECK_INTERVAL
            )

    except Exception as e:
        print(
            "HEDGE TRACKING ERROR:",
            e
        )

        traceback.print_exc()

        logs_process(
            "HEDGE TRACKING ERROR: "
            + str(e),
            symbol,
            True
        )


def place_tpsl_orders(
        symbol,
        position_side,
        quantity,
        take_profit,
        stop_loss,
        side,
        intermediate_take,
        intermediate_take_extra,
        fair_price,
        current_price,
        avg_price
):
    try:
        if position_side == "LONG":
            closed_position = "SELL"
        else:
            closed_position = "BUY"

        real_qty = get_position_size(
            symbol,
            position_side
        )

        if real_qty <= 0:
            print(
                "NO POSITION FOR TP/SL:",
                symbol,
                position_side
            )

            logs_process(
                "NO POSITION FOR TP/SL "
                + symbol,
                symbol,
                True
            )

            return

        real_qty = round(
            float(real_qty),
            6
        )

        half_quantity = round(
            real_qty / 2,
            6
        )

        extra_quantity = round(
            real_qty * 0.2,
            6
        )

        msg_tp = None
        msg_sl = None
        msg_tpitt = None

        tp_params = {
            "symbol": symbol,
            "side": closed_position,
            "positionSide": position_side,
            "type": "TAKE_PROFIT_MARKET",
            "quantity": str(real_qty),
            "stopPrice": str(
                round(
                    take_profit,
                    6
                )
            ),
            "workingType": "CONTRACT_PRICE"
        }

        sl_params = {
            "symbol": symbol,
            "side": closed_position,
            "positionSide": position_side,
            "type": "STOP_MARKET",
            "quantity": str(real_qty),
            "stopPrice": str(
                round(
                    stop_loss,
                    6
                )
            ),
            "workingType": "CONTRACT_PRICE"
        }

        print(
            "PLACE TP PARAMS:",
            tp_params
        )

        print(
            "PLACE SL PARAMS:",
            sl_params
        )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=2
        ) as executor:

            fut_tp = executor.submit(
                create_order_with_retries,
                tp_params,
                3,
                0.7
            )

            fut_sl = executor.submit(
                create_order_with_retries,
                sl_params,
                3,
                0.7
            )

            tp = fut_tp.result()
            sl = fut_sl.result()

        print(
            "MAIN TAKE PROFIT RESPONSE:",
            tp
        )

        print(
            "MAIN STOP LOSS RESPONSE:",
            sl
        )

        logs_process(
            "MAIN TAKE PROFIT RESPONSE: "
            + str(tp),
            symbol
        )

        logs_process(
            "MAIN STOP LOSS RESPONSE: "
            + str(sl),
            symbol
        )

        if (
            tp
            and tp.get("code") == 0
        ):
            msg_tp = (
                tp
                .get("data", {})
                .get("order", {})
                .get("orderId")
            )

        if (
            sl
            and sl.get("code") == 0
        ):
            msg_sl = (
                sl
                .get("data", {})
                .get("order", {})
                .get("orderId")
            )

        if not msg_tp or not msg_sl:
            print(
                "CRITICAL TP/SL ERROR:",
                symbol,
                "tp=",
                tp,
                "sl=",
                sl
            )

            logs_process(
                "CRITICAL TP/SL ERROR "
                "TP="
                + str(tp)
                + " SL="
                + str(sl),
                symbol,
                True
            )

            cancel_order_safely(
                symbol,
                msg_tp
            )

            cancel_order_safely(
                symbol,
                msg_sl
            )

            close_res = close_position_market(
                symbol,
                position_side
            )

            logs_process(
                "EMERGENCY CLOSE BECAUSE TP/SL FAILED: "
                + str(close_res),
                symbol,
                True
            )

            with position_lock:
                ACTIVE_POSITIONS.pop(
                    symbol,
                    None
                )

            return

        if (
            intermediate_take != 0
            and half_quantity > 0
        ):
            tp_intermediate = (
                create_order_with_retries(
                    {
                        "symbol": symbol,
                        "side": closed_position,
                        "positionSide": position_side,
                        "type": "TAKE_PROFIT_MARKET",
                        "quantity": str(
                            half_quantity
                        ),
                        "stopPrice": str(
                            round(
                                intermediate_take,
                                6
                            )
                        ),
                        "workingType": "CONTRACT_PRICE"
                    },
                    retries=3,
                    delay=0.7
                )
            )

            print(
                "INTERMEDIATE TP RESPONSE:",
                tp_intermediate
            )

            logs_process(
                "INTERMEDIATE TP RESPONSE: "
                + str(tp_intermediate),
                symbol
            )

            if (
                tp_intermediate
                and tp_intermediate.get("code") == 0
            ):
                msg_tpitt = (
                    tp_intermediate
                    .get("data", {})
                    .get("order", {})
                    .get("orderId")
                )

        if (
            intermediate_take_extra != 0
            and extra_quantity > 0
        ):
            extra_tp_intermediate = (
                create_order_with_retries(
                    {
                        "symbol": symbol,
                        "side": closed_position,
                        "positionSide": position_side,
                        "type": "TAKE_PROFIT_MARKET",
                        "quantity": str(
                            extra_quantity
                        ),
                        "stopPrice": str(
                            round(
                                intermediate_take_extra,
                                6
                            )
                        ),
                        "workingType": "CONTRACT_PRICE"
                    },
                    retries=3,
                    delay=0.7
                )
            )

            print(
                "EXTRA INTERMEDIATE TP RESPONSE:",
                extra_tp_intermediate
            )

            logs_process(
                "EXTRA INTERMEDIATE TP RESPONSE: "
                + str(extra_tp_intermediate),
                symbol
            )

        with position_lock:
            ACTIVE_POSITIONS.setdefault(
                symbol,
                {}
            )

            ACTIVE_POSITIONS[
                symbol
            ]["positionSide"] = position_side

            ACTIVE_POSITIONS[
                symbol
            ]["stop_loss"] = msg_sl

            ACTIVE_POSITIONS[
                symbol
            ]["take_profit"] = msg_tp

            ACTIVE_POSITIONS[
                symbol
            ]["intermediate_tp"] = msg_tpitt

            ACTIVE_POSITIONS[
                symbol
            ]["entry_price"] = avg_price

            ACTIVE_POSITIONS[
                symbol
            ]["hedge_started"] = True

            ACTIVE_POSITIONS[
                symbol
            ]["hedge_active"] = False

            ACTIVE_POSITIONS[
                symbol
            ]["hedge_side"] = None

            ACTIVE_POSITIONS[
                symbol
            ]["hedge_open_price"] = None

            ACTIVE_POSITIONS[
                symbol
            ]["hedge_open_order"] = None

            ACTIVE_POSITIONS[
                symbol
            ]["hedge_exit_order"] = None

        print(
            "ACTIVE_POSITIONS AFTER TP/SL:",
            ACTIVE_POSITIONS
        )

        logs_process(
            "ACTIVE POSITION AFTER TP/SL: "
            + str(
                ACTIVE_POSITIONS[symbol]
            ),
            symbol,
            True
        )

        if intermediate_take != 0:
            threading.Thread(
                target=tracking_stop_nolose,
                args=(
                    symbol,
                    position_side,
                    real_qty,
                    closed_position,
                    intermediate_take,
                    fair_price
                ),
                daemon=True
            ).start()

        start_hedge_thread = False

        with position_lock:
            if symbol in ACTIVE_POSITIONS:
                if not ACTIVE_POSITIONS[
                    symbol
                ].get(
                    "hedge_thread_started"
                ):
                    ACTIVE_POSITIONS[
                        symbol
                    ][
                        "hedge_thread_started"
                    ] = True

                    ACTIVE_POSITIONS[
                        symbol
                    ][
                        "hedge_active"
                    ] = False

                    ACTIVE_POSITIONS[
                        symbol
                    ][
                        "hedge_side"
                    ] = None

                    start_hedge_thread = True

        if start_hedge_thread:
            threading.Thread(
                target=tracking_hedge_mode,
                args=(
                    symbol,
                    stop_loss,
                    avg_price,
                    position_side
                ),
                daemon=True
            ).start()

    except Exception as e:
        print(
            "PLACE_TPSL_ORDERS ERROR:",
            e
        )

        traceback.print_exc()

        logs_process(
            "PLACE_TPSL_ORDERS ERROR: "
            + str(e),
            symbol,
            True
        )


def open_position(
        ticker,
        side,
        position_side,
        leverage,
        quantity,
        level,
        current_price,
        retries=2
):
    symbol_lock = get_symbol_lock(
        ticker
    )

    with symbol_lock:
        try:
            attempt = 0
            skip_open = False
            order = None

            level = float(level)
            current_price = float(
                current_price
            )

            print(
                "OPEN_POSITION START:",
                ticker,
                "side=",
                side,
                "position_side=",
                position_side,
                "quantity=",
                quantity,
                "level=",
                level,
                "current_price=",
                current_price
            )

            while attempt < retries:
                entry_allowed = (
                    (
                        level > current_price
                        and position_side == "LONG"
                    )
                    or
                    (
                        level < current_price
                        and position_side == "SHORT"
                    )
                )

                if not entry_allowed:
                    print(
                        "ENTRY CONDITION FAILED"
                    )

                    logs_process(
                        "ENTRY CONDITION FAILED",
                        ticker,
                        True
                    )

                    return

                leverage_response = send_request(
                    "/openApi/swap/v2/trade/leverage",
                    {
                        "symbol": ticker,
                        "side": position_side,
                        "leverage": leverage,
                        "timestamp": now_ms()
                    }
                )

                print(
                    "LEVERAGE RESPONSE:",
                    leverage_response
                )

                logs_process(
                    "LEVERAGE RESPONSE: "
                    + str(leverage_response),
                    ticker
                )

                if (
                    not leverage_response
                    or leverage_response.get("code") != 0
                ):
                    logs_process(
                        "LEVERAGE ERROR: "
                        + str(leverage_response),
                        ticker,
                        True
                    )

                    return

                params = {
                    "symbol": ticker,
                    "side": side,
                    "positionSide": position_side,
                    "type": "MARKET",
                    "quantity": str(quantity),
                    "recvWindow": "5000"
                }

                with position_lock:
                    if ticker in global_banlist:
                        print(
                            "TICKER IN GLOBAL BANLIST:",
                            ticker
                        )

                        logs_process(
                            ticker
                            + " IN GLOBAL BANLIST",
                            ticker,
                            True
                        )

                        return

                    current_position = (
                        ACTIVE_POSITIONS.get(
                            ticker,
                            {}
                        )
                    )

                    current_position_side = (
                        current_position.get(
                            "positionSide"
                        )
                    )

                    same_position_exists = (
                        current_position_side
                        == position_side
                    )

                    opposite_position_exists = (
                        current_position_side is not None
                        and current_position_side
                        != position_side
                    )

                    if same_position_exists:
                        print(
                            "SAME POSITION EXISTS. "
                            "REFRESH TP/SL:",
                            ticker
                        )

                        logs_process(
                            "SAME POSITION EXISTS. "
                            "REFRESH TP/SL",
                            ticker
                        )

                        for order_key in (
                            "take_profit",
                            "stop_loss",
                            "intermediate_tp"
                        ):
                            order_id = (
                                current_position.get(
                                    order_key
                                )
                            )

                            if order_id:
                                response = (
                                    cancel_order_safely(
                                        ticker,
                                        order_id
                                    )
                                )

                                logs_process(
                                    f"OLD {order_key} "
                                    f"CANCELLED "
                                    f"({order_id}): "
                                    f"{response}",
                                    ticker
                                )

                        ACTIVE_POSITIONS[
                            ticker
                        ]["take_profit"] = None

                        ACTIVE_POSITIONS[
                            ticker
                        ]["stop_loss"] = None

                        ACTIVE_POSITIONS[
                            ticker
                        ]["intermediate_tp"] = None

                        skip_open = True

                    elif opposite_position_exists:
                        print(
                            "OPPOSITE POSITION EXISTS. "
                            "IGNORE SIGNAL:",
                            ticker,
                            current_position_side,
                            "->",
                            position_side
                        )

                        logs_process(
                            f"IGNORE OPPOSITE SIGNAL: "
                            f"existing={current_position_side}, "
                            f"requested={position_side}",
                            ticker,
                            True
                        )

                        return

                if skip_open:
                    break

                order = create_order_with_retries(
                    params,
                    retries=3,
                    delay=0.7
                )

                print(
                    "OPEN ORDER RESPONSE:",
                    order
                )

                logs_process(
                    "OPEN ORDER "
                    + str(order),
                    ticker
                )

                if (
                    order
                    and order.get("code") == 0
                ):
                    with position_lock:
                        ACTIVE_POSITIONS.setdefault(
                            ticker,
                            {}
                        )

                        ACTIVE_POSITIONS[
                            ticker
                        ]["positionSide"] = (
                            position_side
                        )

                        ACTIVE_POSITIONS[
                            ticker
                        ]["orderID"] = (
                            order
                            .get("data", {})
                            .get("order", {})
                            .get("orderId")
                        )

                        ACTIVE_POSITIONS[
                            ticker
                        ]["take_profit"] = None

                        ACTIVE_POSITIONS[
                            ticker
                        ]["stop_loss"] = None

                        ACTIVE_POSITIONS[
                            ticker
                        ]["intermediate_tp"] = None

                    break

                error_msg = ""

                if order:
                    error_msg = str(
                        order.get(
                            "msg",
                            ""
                        )
                    ).lower()

                logs_process(
                    "OPEN ERROR: "
                    + error_msg,
                    ticker,
                    True
                )

                if (
                    order
                    and order.get("code") == 109400
                ):
                    global_banlist.add(
                        ticker
                    )

                    save_banlist()

                    logs_process(
                        ticker
                        + " ADDED TO GLOBAL BANLIST",
                        ticker,
                        True
                    )

                    return

                if "liquidation" in error_msg:
                    print(
                        "HIGH LIQUIDATION RISK"
                    )
                    return

                attempt += 1

                time.sleep(1)

            if skip_open:
                positions_request = send_request(
                    "/openApi/swap/v2/user/positions",
                    {
                        "symbol": ticker,
                        "timestamp": now_ms()
                    },
                    method="GET"
                )

                avg_price = None

                for pos in positions_request.get(
                    "data",
                    []
                ):
                    if (
                        pos.get("symbol") == ticker
                        and pos.get(
                            "positionSide"
                        ) == position_side
                        and float(
                            pos.get(
                                "positionAmt",
                                0
                            )
                        ) != 0
                    ):
                        avg_price = float(
                            pos.get(
                                "avgPrice"
                            )
                        )

                        break

                if not avg_price:
                    print(
                        "FAILED TO GET AVG PRICE"
                    )

                    logs_process(
                        "FAILED TO GET AVG PRICE",
                        ticker,
                        True
                    )

                    return

            else:
                if (
                    not order
                    or order.get("code") != 0
                ):
                    print(
                        "FAILED TO OPEN POSITION"
                    )

                    logs_process(
                        "FAILED TO OPEN POSITION",
                        ticker,
                        True
                    )

                    return

                avg_price = float(
                    order
                    .get("data", {})
                    .get("order", {})
                    .get("avgPrice", 0)
                )

                if avg_price <= 0:
                    positions_request = send_request(
                        "/openApi/swap/v2/user/positions",
                        {
                            "symbol": ticker,
                            "timestamp": now_ms()
                        },
                        method="GET"
                    )

                    for pos in positions_request.get(
                        "data",
                        []
                    ):
                        if (
                            pos.get("symbol") == ticker
                            and pos.get(
                                "positionSide"
                            ) == position_side
                            and float(
                                pos.get(
                                    "positionAmt",
                                    0
                                )
                            ) != 0
                        ):
                            avg_price = float(
                                pos.get(
                                    "avgPrice"
                                )
                            )

                            break

                if avg_price <= 0:
                    print(
                        "AVG PRICE INVALID:",
                        ticker
                    )

                    logs_process(
                        "AVG PRICE INVALID",
                        ticker,
                        True
                    )

                    return

            if position_side == "LONG":
                take_profit = level * 0.996
                stop_loss = avg_price * 0.96

            elif position_side == "SHORT":
                take_profit = level * 1.004
                stop_loss = avg_price * 1.04

            else:
                print(
                    "UNKNOWN POSITION SIDE"
                )

                return

            intermediate_take = 0
            intermediate_take_extra = 0

            take_profit = float(
                f"{take_profit:.6f}"
            )

            stop_loss = float(
                f"{stop_loss:.6f}"
            )

            avg_price = float(
                f"{avg_price:.6f}"
            )

            logs_process(
                f"AVG={avg_price} || "
                f"TP={take_profit} || "
                f"SL={stop_loss} || "
                f"INT_TP={intermediate_take}",
                ticker
            )

            place_tpsl_orders(
                ticker,
                position_side,
                quantity,
                take_profit,
                stop_loss,
                side,
                intermediate_take,
                intermediate_take_extra,
                level,
                current_price,
                avg_price
            )

            return order

        except Exception as e:
            print(
                "OPEN_POSITION ERROR:",
                e
            )

            traceback.print_exc()

            logs_process(
                "OPEN_POSITION ERROR: "
                + str(e),
                ticker,
                True
            )

            return


@bot.on(
    events.NewMessage(
        chats=SIGNAL_CHAT
    )
)
async def handler(event):
    try:
        text = event.message.message

        if not text:
            return

        lines = text.split("\n")

        ticker_match = re.search(
            r"(?:-\s*)?([A-Z0-9]+)USDT\b",
            text,
            re.IGNORECASE
        )

        if not ticker_match:
            print(
                "HANDLER: TICKER NOT FOUND"
            )

            print(text)

            return

        ticker = (
            ticker_match
            .group(1)
            .upper()
            + "-USDT"
        )

        levels = [
            float(x)
            for x in re.findall(
                r"(\d+\.\d+)\$",
                text
            )
        ]

        if not levels:
            print(
                f"HANDLER: LEVELS NOT FOUND "
                f"FOR {ticker}"
            )

            print(text)

            return

        level = max(levels)

        current_price = get_current_price(
            ticker
        )

        if current_price is None:
            print(
                "HANDLER: CURRENT PRICE ERROR:",
                ticker
            )

            return

        if float(level) > float(
            current_price
        ):
            side = "BUY"
            position_side = "LONG"

        elif float(level) < float(
            current_price
        ):
            side = "SELL"
            position_side = "SHORT"

        else:
            print(
                "NO DIRECTION"
            )

            return

        quantity_full = (
            usdt / current_price
        )

        quantity_full = round(
            quantity_full,
            8
        )

        logs_process(
            "BLOCKED POSITIONS: "
            + str(global_banlist),
            ticker
        )

        logs_process(
            str(time.time())
            + " SIDE="
            + side
            + " QTY="
            + str(quantity_full)
            + " PRICE="
            + str(current_price),
            ticker
        )

        logs_process(
            "",
            ticker
        )

        if (
            ticker not in all_signals
            or all_signals.count(ticker) <= 2
            or (
                ticker in all_signals
                and ticker in white_list
            )
        ):
            open_position(
                ticker,
                side,
                position_side,
                leverage,
                quantity_full,
                level,
                current_price
            )

            all_signals.append(
                ticker
            )

        else:
            print(
                "OPEN POSITION CONDITION FAILED"
            )

    except Exception as e:
        print(
            "HANDLER ERROR:",
            e
        )

        print(
            traceback.format_exc()
        )


print(
    "BAN LIST GLOBAL:",
    global_banlist
)

print(
    "BAN LIST LOCAL:",
    local_banlist
)

print(
    "WHITE LIST:",
    white_list
)


threading.Thread(
    target=sync_positions,
    daemon=True
).start()


bot.start(
    phone=PHONE
)


@bot.on(
    events.NewMessage(
        pattern=r"^/ban"
    )
)
async def ban_handler(event):
    parts = (
        event.message.message
        .strip()
        .split()
    )

    if (
        len(parts) == 1
        or parts[1] == "commands"
    ):
        await event.reply(
            "commands:\n"
            "/ban list\n"
            "/ban add BTC-USDT\n"
            "/ban del BTC-USDT\n"
            "/ban clear\n"
            "/set leverage LEVERAGE\n"
            "/set usdt MARGIN"
        )

    elif parts[1] == "list":
        if global_banlist:
            msg = (
                "Global banlist:\n"
                + "\n".join(
                    sorted(global_banlist)
                )
            )

        else:
            msg = "Global banlist is empty"

        await event.reply(msg)

    elif (
        parts[1] == "add"
        and len(parts) >= 3
    ):
        symbol = parts[2].upper()

        global_banlist.add(
            symbol
        )

        save_banlist()

        await event.reply(
            f"{symbol} added to global banlist"
        )

        logs_process(
            symbol
            + " added to GLOBAL BLOCK LIST",
            symbol,
            True
        )

    elif (
        parts[1] == "del"
        and len(parts) >= 3
    ):
        symbol = parts[2].upper()

        if symbol in global_banlist:
            global_banlist.discard(
                symbol
            )

            save_banlist()

            await event.reply(
                f"{symbol} removed from global banlist"
            )

            logs_process(
                symbol
                + " removed from GLOBAL BLOCK LIST",
                symbol,
                True
            )

        else:
            await event.reply(
                f"{symbol} not found in banlist"
            )

    elif parts[1] == "clear":
        global_banlist.clear()

        save_banlist()

        await event.reply(
            "Banlist cleared"
        )


bot.run_until_disconnected()