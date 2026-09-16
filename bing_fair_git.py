import os
import re
import urllib.parse
import requests
import time
import hmac
import hashlib
import threading
import concurrent.futures
import traceback
import argparse
import json

from datetime import datetime, timedelta

import socks
from dotenv import load_dotenv
from telethon import TelegramClient, events


load_dotenv()

os.environ["PYTHONASYNCIODEBUG"] = "1"


parser = argparse.ArgumentParser()
parser.add_argument("--usdt", type=float)
parser.add_argument("--leverage", type=int)

args = parser.parse_args()


POSITION_USDT = args.usdt if args.usdt is not None else float(
    os.getenv("POSITION_USDT", "5")
)

LEVERAGE = args.leverage if args.leverage is not None else int(
    os.getenv("LEVERAGE", "5")
)


API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("PHONE")

PROXY_HOST = os.getenv("PROXY_HOST")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8000"))
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")

proxy = (
    socks.HTTP,
    PROXY_HOST,
    PROXY_PORT,
    True,
    PROXY_USERNAME,
    PROXY_PASSWORD
)

SESSION_NAME = os.getenv(
    "TELEGRAM_SESSION",
    "Trading_Bot_Fair_BingXU.session"
)

SIGNAL_CHAT = os.getenv("SIGNAL_CHAT", "bing_9p")

bot = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH,
    proxy=proxy
)


BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET")
BASE_URL = "https://open-api.bingx.com"


API_LOG = os.getenv("TELEGRAM_BOT_TOKEN")

LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID"))

PROXY_URL = (
    f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}"
    f"@{PROXY_HOST}:{PROXY_PORT}"
)

proxies = {
    "http": PROXY_URL,
    "https": PROXY_URL,
}


list_logs = {}
ACTIVE_POSITIONS = {}
local_banlist = {}
all_signals = []
white_list = set()

BANLIST_FILE = "global_banlist.json"


def load_banlist():
    global global_banlist

    if not os.path.exists(BANLIST_FILE):
        return

    with open(BANLIST_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    saved_time = datetime.fromisoformat(data["saved_at"])

    if datetime.now() - saved_time > timedelta(days=3):
        print("BANLIST CLEARED")
        global_banlist = set()
        save_banlist()
    else:
        global_banlist = set(data["symbols"])
        print("BANLIST LOADED:", global_banlist)


def save_banlist():
    with open(BANLIST_FILE, "w", encoding="utf-8") as f:
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


def send_file_to_telegram(filepath):
    url = f"https://api.telegram.org/bot{API_LOG}/sendPhoto"

    with open(filepath, "r", encoding="utf-8") as file:
        text = file.read()

    with open("bingphoto.jpg", "rb") as photo:
        requests.post(
            url,
            data={
                "chat_id": LOG_CHAT_ID,
                "caption": text[:1000]
            },
            files={
                "photo": photo
            },
            proxies=proxies,
            timeout=30
        )


def logs_process(message, symbol, status=False):
    if symbol not in list_logs:
        list_logs[symbol] = []

    list_logs[symbol].append(str(message) + "\n")

    if status:
        with open("logsbing17", "w", encoding="utf-8") as file:
            file.writelines(list_logs[symbol])

        send_file_to_telegram("logsbing17")

        list_logs[symbol].clear()


def get_position_size(symbol, position_side):
    res = send_request(
        "/openApi/swap/v2/user/positions",
        {
            "symbol": symbol,
            "timestamp": str(int(time.time() * 1000))
        },
        method="GET"
    )

    positions = res.get("data", [])

    for p in positions:
        if (
            p["symbol"] == symbol
            and p["positionSide"] == position_side
        ):
            return abs(float(p["positionAmt"]))

    return 0


def get_current_price(symbol):
    response = send_request(
        "/openApi/swap/v2/quote/ticker",
        {
            "symbol": symbol
        },
        method="GET"
    )

    return float(response["data"]["lastPrice"])


def get_last_closed_pnl(symbol):
    response = send_request(
        "/openApi/swap/v2/trade/allOrders",
        {
            "symbol": symbol,
            "timestamp": str(int(time.time() * 1000))
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
            print("FOUND CLOSED PNL:", profit)
            return profit

    return 0


def sign_string(query_string):
    return hmac.new(
        BINGX_API_SECRET.encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()


def send_request(path, params, method="POST"):
    try:
        headers = {
            "X-BX-APIKEY": BINGX_API_KEY
        }

        params = {
            k: str(v)
            for k, v in params.items()
        }

        query_string = urllib.parse.urlencode(params)

        signature = sign_string(query_string)

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
                headers=headers
            )

        elif method == "POST":
            response = requests.post(
                url,
                headers=headers
            )

        elif method == "DELETE":
            response = requests.delete(
                url,
                headers=headers
            )

        else:
            raise ValueError(
                f"Unsupported method: {method}"
            )

        return response.json()

    except Exception as e:
        if "Read timed out." not in str(e):
            print("Ошибка в send_request:", e)

        return {}


def return_pnl(order_id, symbol):
    response_pnl = send_request(
        "/openApi/swap/v2/trade/order",
        {
            "symbol": symbol,
            "orderId": order_id,
            "timestamp": str(int(time.time() * 1000))
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
            f"НЕ УДАЛОСЬ ПОЛУЧИТЬ PNL {symbol}"
        )
        return

    print("PNL:", profit)

    is_negative = profit < 0

    sorting_banlist(
        symbol,
        is_negative
    )

    logs_process(
        symbol + " " + str(profit) + " С ПРОФИТОМ!",
        symbol,
        True
    )


def sorting_banlist(symbol, is_negative):
    global all_signals

    if not is_negative:
        print("PNL положительный")
        print("WHITE LIST BEFORE:", white_list)

        white_list.add(symbol)

        all_signals = [
            x for x in all_signals
            if x != symbol
        ]

        print("WHITE LIST:", white_list)

        return

    local_banlist[symbol] = (
        local_banlist.get(symbol, 0) + 1
    )

    print(
        "PNL отрицательный:",
        local_banlist
    )

    if local_banlist[symbol] >= 2:
        logs_process(
            symbol + " был добавлен в GLOBAL BLOCK LIST",
            symbol,
            True
        )

        print(
            symbol + " был добавлен в GLOBAL BLOCK LIST"
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
                    "timestamp": str(
                        int(time.time() * 1000)
                    )
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
                    position_side = pos["positionSide"]

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
                                f"FALSE CLOSE DETECTED {ticker}"
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
                                f"позиция удалена принудительно "
                                f"(PNL не найден) {ticker}",
                                ticker,
                                True
                            )

                            del ACTIVE_POSITIONS[ticker]

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
                            f"позиция подтверждённо закрыта "
                            f"{ticker}",
                            ticker,
                            True
                        )

                        del ACTIVE_POSITIONS[ticker]

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
    last_price,
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
            + " начался трекинг позиции, "
            + "tracking_stop_nolose",
            symbol,
            True
        )

        quantity = float(quantity)

        half_quantity = round(
            quantity / 2,
            6
        )

        max_checks = 5000
        checks = 0

        while True:
            positions_request = send_request(
                "/openApi/swap/v2/user/positions",
                {
                    "symbol": symbol,
                    "timestamp": str(
                        int(time.time() * 1000)
                    )
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
                    and float(pos["positionAmt"]) != 0
                ):
                    active_position = pos
                    break

            if active_position is None:
                print("POSITION CLOSED")
                break

            current_price = get_current_price(
                symbol
            )

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
                print("MOVE SL")

                logs_process(
                    symbol + " SL ПОДВИНУТ",
                    symbol,
                    True
                )

                open_orders = send_request(
                    "/openApi/swap/v2/trade/openOrders",
                    {
                        "symbol": symbol,
                        "timestamp": str(
                            int(time.time() * 1000)
                        )
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
                        order.get("type") == "STOP_MARKET"
                        and order.get("positionSide") == position_side
                    ):
                        send_request(
                            "/openApi/swap/v2/trade/order",
                            {
                                "symbol": symbol,
                                "orderId": order["orderId"],
                                "timestamp": str(
                                    int(time.time() * 1000)
                                )
                            },
                            method="DELETE"
                        )

                        print("OLD SL DELETED")

                        logs_process(
                            symbol + " СТАРЫЙ СЛ УДАЛЕН",
                            symbol,
                            True
                        )

                time.sleep(0.3)

                if position_side == "LONG":
                    new_sl_price = current_price * 0.97
                else:
                    new_sl_price = current_price * 1.03

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
                        "timestamp": str(
                            int(time.time() * 1000)
                        )
                    }
                )

                if new_sl.get("code") != 0:
                    print(
                        "ОШИБКА СОЗДАНИЯ НОВОГО СТОПЛОССА"
                    )

                    closed_position_bing = send_request(
                        "/openApi/swap/v2/trade/order",
                        {
                            "symbol": symbol,
                            "side": closed_position,
                            "positionSide": position_side,
                            "type": "MARKET",
                            "quantity": str(real_qty),
                            "timestamp": str(
                                int(time.time() * 1000)
                            )
                        }
                    )

                    print(
                        "CLOSE POSITION:",
                        closed_position_bing
                    )

                    break

                print(
                    "NEW BREAKEVEN SL:",
                    new_sl
                )

                logs_process(
                    "новый стоп",
                    symbol,
                    True
                )

                break

            time.sleep(0.5)

        print("TRACKING FINISHED")

    except Exception as e:
        print(
            "TRACKING ERROR:",
            e
        )


def place_tpsl_orders(
    symbol,
    position_side,
    quantity,
    take_profit,
    stop_loss,
    side,
    intermediate_take,
    last_price,
    intermediate_take_extra,
    fair_price,
    current_price
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
            print("NO POSITION")
            return

        half_quantity = round(
            quantity / 2,
            6
        )

        extra_quantity = round(
            quantity * 0.2,
            6
        )

        tp_params = {
            "symbol": symbol,
            "side": closed_position,
            "positionSide": position_side,
            "type": "TAKE_PROFIT_MARKET",
            "quantity": str(real_qty),
            "stopPrice": str(
                round(take_profit, 6)
            ),
            "workingType": "CONTRACT_PRICE",
            "timestamp": str(
                int(time.time() * 1000)
            )
        }

        sl_params = {
            "symbol": symbol,
            "side": closed_position,
            "positionSide": position_side,
            "type": "STOP_MARKET",
            "quantity": str(real_qty),
            "stopPrice": str(
                round(stop_loss, 6)
            ),
            "workingType": "CONTRACT_PRICE",
            "timestamp": str(
                int(time.time() * 1000)
            )
        }

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=2
        ) as executor:

            fut_tp = executor.submit(
                send_request,
                "/openApi/swap/v2/trade/order",
                tp_params
            )

            fut_sl = executor.submit(
                send_request,
                "/openApi/swap/v2/trade/order",
                sl_params
            )

            tp = fut_tp.result()
            sl = fut_sl.result()

        print("ОСНОВНОЙ ТЕЙК:", tp)

        if tp.get("code") != 0:
            print(
                "ОШИБКА TP:",
                tp.get("msg")
            )
            msg_tp = None
        else:
            msg_tp = (
                tp["data"]["order"]["orderId"]
            )

        print("ОСНОВНОЙ STOP:", sl)

        if sl.get("code") != 0:
            print(
                "SL ERROR:",
                sl.get("msg")
            )
            msg_sl = None
        else:
            msg_sl = (
                sl["data"]["order"]["orderId"]
            )

        logs_process(
            "INFO TP "
            + str(tp)
            + " || INFO SL "
            + str(sl),
            symbol
        )

        logs_process(
            "",
            symbol
        )

        if tp.get("code") != 0:
            closed_position_bing = send_request(
                "/openApi/swap/v2/trade/order",
                {
                    "symbol": symbol,
                    "side": closed_position,
                    "positionSide": position_side,
                    "type": "MARKET",
                    "quantity": str(real_qty),
                    "timestamp": str(
                        int(time.time() * 1000)
                    )
                }
            )

            if "should be" in tp.get(
                "msg",
                ""
            ):
                print(
                    "ПОЗИЦИЯ ЗАКРЫТА ИЗ-ЗА SHOULD BE:",
                    closed_position_bing
                )

                logs_process(
                    "ПОЗИЦИЯ ЗАКРЫТА ИЗ-ЗА SHOULD BE",
                    symbol,
                    True
                )
            else:
                print(
                    f"ПОЗИЦИЯ ЗАКРЫТА {tp.get('msg')}",
                    closed_position_bing
                )

                logs_process(
                    f"ПОЗИЦИЯ ЗАКРЫТА {tp.get('msg')}",
                    symbol,
                    True
                )

            if closed_position_bing.get("code") != 0:
                global_banlist.add(symbol)

                logs_process(
                    "НЕОБХОДИМО РУЧНОЕ ЗАКРЫТИЕ ПОЗИЦИИ",
                    symbol,
                    True
                )

                print(
                    "НЕОБХОДИМО РУЧНОЕ ЗАКРЫТИЕ ПОЗИЦИИ"
                )

            return

        msg_tpitt = None

        if intermediate_take != 0:
            tp_intermediate = send_request(
                "/openApi/swap/v2/trade/order",
                {
                    "symbol": symbol,
                    "side": side,
                    "positionSide": position_side,
                    "type": "TAKE_PROFIT_MARKET",
                    "quantity": str(half_quantity),
                    "stopPrice": str(
                        round(intermediate_take, 6)
                    ),
                    "workingType": "CONTRACT_PRICE",
                    "timestamp": str(
                        int(time.time() * 1000)
                    )
                }
            )

            logs_process(
                "INFO intermediate TP "
                + str(tp_intermediate),
                symbol
            )

            logs_process(
                "",
                symbol
            )

            if tp_intermediate.get("code") != 0:
                print(
                    "INTERMEDIATE TP ERROR:",
                    tp_intermediate.get("msg")
                )
            else:
                msg_tpitt = (
                    tp_intermediate["data"]["order"]["orderId"]
                )

        else:
            logs_process(
                "LOGS ARE FINISHED",
                symbol,
                True
            )

        ACTIVE_POSITIONS[symbol]["stop_loss"] = msg_sl
        ACTIVE_POSITIONS[symbol]["take_profit"] = msg_tp
        ACTIVE_POSITIONS[symbol]["intermediate_tp"] = msg_tpitt

        print(
            "ACTIVE_POSITIONS:",
            ACTIVE_POSITIONS
        )

        logs_process(
            "актив позиции после открытия ордеров "
            + str(ACTIVE_POSITIONS[symbol]),
            symbol,
            True
        )

        logs_process(
            "",
            symbol
        )

        if intermediate_take_extra != 0:
            extra_tp_intermediate = send_request(
                "/openApi/swap/v2/trade/order",
                {
                    "symbol": symbol,
                    "side": side,
                    "positionSide": position_side,
                    "type": "TAKE_PROFIT_MARKET",
                    "quantity": str(extra_quantity),
                    "stopPrice": str(
                        round(intermediate_take_extra, 6)
                    ),
                    "workingType": "CONTRACT_PRICE",
                    "timestamp": str(
                        int(time.time() * 1000)
                    )
                }
            )

            print(
                "ПРОМЕЖУТОЧНЫЙ EXTRA ТЕЙК:",
                extra_tp_intermediate
            )

        if intermediate_take != 0:
            threading.Thread(
                target=tracking_stop_nolose,
                args=(
                    symbol,
                    position_side,
                    quantity,
                    last_price,
                    side,
                    intermediate_take,
                    fair_price
                ),
                daemon=True
            ).start()

    except Exception as e:
        print(
            "ОШИБКА OPEN_POSITION:",
            e
        )


print(
    "ACTIVE_POSITIONS:",
    ACTIVE_POSITIONS
)


def open_position(
    ticker,
    side,
    position_side,
    leverage,
    quantity,
    fair_price,
    last_price,
    percentage,
    current_price,
    retries=2
):
    try:
        attempt = 0
        skip_open = False
        order = None
        closed_side_opt = ""

        while attempt < retries:
            if (
                (
                    last_price < fair_price
                    and position_side == "LONG"
                )
                or
                (
                    last_price > fair_price
                    and position_side == "SHORT"
                )
            ):

                leverage_response = send_request(
                    "/openApi/swap/v2/trade/leverage",
                    {
                        "symbol": ticker,
                        "leverage": leverage
                    }
                )

                params = {
                    "symbol": ticker,
                    "side": side,
                    "positionSide": position_side,
                    "type": "MARKET",
                    "quantity": str(quantity),
                    "timestamp": str(
                        int(time.time() * 1000)
                    ),
                    "recvWindow": "5000"
                }

                print(
                    "PARAMS:",
                    params
                )

                logs_process(
                    "АКТИВНЫЕ ПОЗИЦИИ: "
                    + str(ACTIVE_POSITIONS),
                    ticker
                )

                logs_process(
                    "",
                    ticker
                )

                with position_lock:
                    if (
                        ticker not in ACTIVE_POSITIONS
                        or position_side
                        == ACTIVE_POSITIONS[ticker]["positionSide"]
                    ):

                        if (
                            ticker in ACTIVE_POSITIONS
                            and position_side
                            == ACTIVE_POSITIONS[ticker]["positionSide"]
                        ):

                            for order_key in [
                                "take_profit",
                                "stop_loss",
                                "intermediate_tp"
                            ]:

                                order_id = (
                                    ACTIVE_POSITIONS[ticker]
                                    .get(order_key)
                                )

                                if order_id:
                                    res = send_request(
                                        "/openApi/swap/v2/trade/order",
                                        {
                                            "symbol": ticker,
                                            "orderId": str(order_id),
                                            "timestamp": str(
                                                int(
                                                    time.time() * 1000
                                                )
                                            )
                                        },
                                        method="DELETE"
                                    )

                                    print(
                                        f"DELETE {order_key} "
                                        f"({order_id}):",
                                        res
                                    )

                                    logs_process(
                                        f"УДАЛЕНЫ СТАРЫЕ ОРДЕРА "
                                        f"{order_key} ({order_id}): "
                                        + str(res),
                                        ticker
                                    )

                                    logs_process(
                                        "",
                                        ticker
                                    )

                                else:
                                    print(
                                        f"ПРОПУЩЕН {order_key}"
                                    )

                            ACTIVE_POSITIONS[ticker][
                                "stop_loss"
                            ] = None

                            ACTIVE_POSITIONS[ticker][
                                "take_profit"
                            ] = None

                            ACTIVE_POSITIONS[ticker][
                                "intermediate_tp"
                            ] = None

                            skip_open = True

                        if not skip_open:
                            if ticker not in global_banlist:
                                order = send_request(
                                    "/openApi/swap/v2/trade/order",
                                    params
                                )

                                logs_process(
                                    "ОТКРЫВАЕМ ОРДЕР "
                                    + str(order),
                                    ticker
                                )

                                logs_process(
                                    "",
                                    ticker
                                )

                                if order.get("code") == 0:
                                    ACTIVE_POSITIONS.setdefault(
                                        ticker,
                                        {}
                                    )

                                    ACTIVE_POSITIONS[ticker][
                                        "positionSide"
                                    ] = position_side

                                    ACTIVE_POSITIONS[ticker][
                                        "orderID"
                                    ] = (
                                        order["data"]["order"]["orderId"]
                                    )

                                    logs_process(
                                        "active positions после открытия: "
                                        + str(ACTIVE_POSITIONS),
                                        ticker
                                    )

                                    logs_process(
                                        "",
                                        ticker
                                    )

                                    break
                            else:
                                print(
                                    global_banlist
                                )

                                error_msg = order.get(
                                    "msg",
                                    ""
                                ).lower()

                                logs_process(
                                    f"ошибка открытия {error_msg}",
                                    ticker,
                                    True
                                )

                                if "liquidation" in error_msg:
                                    print(
                                        "HIGH LIQUIDATION RISK"
                                    )
                                else:
                                    print(
                                        "НЕИЗВЕСТНАЯ ОШИБКА ОТКРЫТИЯ"
                                    )

                                attempt += 1
                                time.sleep(1)
                                continue

                    else:
                        closed_side_opt = (
                            ACTIVE_POSITIONS[ticker][
                                "positionSide"
                            ]
                        )

                        closed_position_1 = (
                            "SELL"
                            if closed_side_opt == "LONG"
                            else "BUY"
                        )

                        for order_key in [
                            "take_profit",
                            "stop_loss",
                            "intermediate_tp"
                        ]:

                            order_id = (
                                ACTIVE_POSITIONS[ticker]
                                .get(order_key)
                            )

                            if order_id:
                                send_request(
                                    "/openApi/swap/v2/trade/order",
                                    {
                                        "symbol": ticker,
                                        "orderId": str(order_id),
                                        "timestamp": str(
                                            int(
                                                time.time() * 1000
                                            )
                                        )
                                    },
                                    method="DELETE"
                                )

                        closed_position_bing = send_request(
                            "/openApi/swap/v2/trade/order",
                            {
                                "symbol": ticker,
                                "side": closed_position_1,
                                "positionSide": closed_side_opt,
                                "type": "MARKET",
                                "quantity": str(quantity),
                                "timestamp": str(
                                    int(time.time() * 1000)
                                )
                            }
                        )

                        print(
                            "ЗАКРЫТА СТАРАЯ ПОЗИЦИЯ:",
                            closed_position_bing
                        )

                        logs_process(
                            "закрытый ордер старый из-за "
                            "смены направления "
                            + str(closed_position_bing),
                            ticker
                        )

                        logs_process(
                            "",
                            ticker
                        )

                        if (
                            closed_position_bing.get("code") == 0
                            or
                            closed_position_bing.get("code") == 101205
                        ):
                            del ACTIVE_POSITIONS[ticker]
                        else:
                            logs_process(
                                "не удалось закрыть старую позицию",
                                ticker,
                                True
                            )

                            return

                        order = send_request(
                            "/openApi/swap/v2/trade/order",
                            params
                        )

                        print(
                            "НОВЫЙ ОРДЕР ПОСЛЕ РАЗВОРОТА:",
                            order
                        )

                        if order.get("code") == 0:
                            ACTIVE_POSITIONS.setdefault(
                                ticker,
                                {}
                            )

                            ACTIVE_POSITIONS[ticker][
                                "positionSide"
                            ] = position_side

                            break

                        else:
                            error_msg = order.get(
                                "msg",
                                ""
                            ).lower()

                            logs_process(
                                "ошибка открытия после разворота "
                                + error_msg,
                                ticker,
                                True
                            )

                            attempt += 1
                            time.sleep(1)
                            continue

            else:
                print(
                    "УСЛОВИЕ ВХОДА НЕ ПРОШЛО"
                )
                return

            if skip_open:
                break

        if skip_open:
            positions_request = send_request(
                "/openApi/swap/v2/user/positions",
                {
                    "symbol": ticker,
                    "timestamp": str(
                        int(time.time() * 1000)
                    )
                },
                method="GET"
            )

            avg_price = None

            for pos in positions_request.get(
                "data",
                []
            ):
                if (
                    pos["symbol"] == ticker
                    and pos["positionSide"] == position_side
                    and float(pos["positionAmt"]) != 0
                ):
                    avg_price = float(
                        pos["avgPrice"]
                    )
                    break

            if not avg_price:
                print(
                    "НЕ УДАЛОСЬ ПОЛУЧИТЬ avg_price"
                )
                return

        else:
            if not order:
                print(
                    "ORDER НЕ СОЗДАН"
                )
                return

            if order.get("code") != 0:
                print(
                    "НЕ УДАЛОСЬ ОТКРЫТЬ ПОЗИЦИЮ"
                )
                return

            avg_price = float(
                order["data"]["order"]["avgPrice"]
            )

        if position_side == "LONG":
            take_profit = fair_price * 0.98
            stop_loss = avg_price * 0.96

        elif position_side == "SHORT":
            take_profit = fair_price * 1.02
            stop_loss = avg_price * 1.04

        else:
            print(
                "НЕ УДАЛОСЬ ОПРЕДЕЛИТЬ POSITION SIDE"
            )
            return

        intermediate_take = 0
        intermediate_take_extra = 0

        if position_side == "LONG":
            if percentage >= 10:
                intermediate_take = (
                    last_price
                    + (
                        take_profit - last_price
                    ) / 2
                )

        elif position_side == "SHORT":
            if percentage >= 10:
                intermediate_take = (
                    last_price
                    - (
                        last_price - take_profit
                    ) / 2
                )

        intermediate_take = float(
            f"{intermediate_take:.6f}"
        )

        take_profit = float(
            f"{take_profit:.6f}"
        )

        stop_loss = float(
            f"{stop_loss:.6f}"
        )

        logs_process(
            "TP: "
            + str(take_profit)
            + " || SL: "
            + str(stop_loss)
            + " || INT TP: "
            + str(intermediate_take),
            ticker
        )

        logs_process(
            "",
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
            last_price,
            intermediate_take_extra,
            fair_price,
            current_price
        )

        return order

    except Exception as e:
        print(
            "ОШИБКА OPEN_POSITION:",
            e
        )
        return


@bot.on(events.NewMessage(chats=SIGNAL_CHAT))
async def handler(event):
    try:
        text = event.message.message

        if not text:
            return

        lines = text.split("\n")
        first_line = lines[0] if lines else ""

        ticker_match = re.search(
            r"\$([A-Z0-9]+)",
            first_line
        )

        last_match = re.search(
            r"Last:\s*\$([\d.]+)",
            text
        )

        fair_match = re.search(
            r"Fair:\s*\$([\d.]+)",
            text
        )

        perc_match = re.search(
            r"Fair:\s*([+-]?\d+(?:\.\d+)?)%",
            text
        )

        if not all(
            [
                ticker_match,
                last_match,
                fair_match,
                perc_match
            ]
        ):
            print(
                "INVALID SIGNAL FORMAT"
            )
            return

        ticker = (
            ticker_match.group(1)
            + "-USDT"
        )

        last_price = float(
            last_match.group(1)
        )

        fair_price = float(
            fair_match.group(1)
        )

        percentage = float(
            perc_match.group(1)
        )

        current_price = get_current_price(
            ticker
        )

        if fair_price > last_price:
            side = "BUY"
            position_side = "LONG"
            fair_price *= 0.99

        elif fair_price < last_price:
            side = "SELL"
            position_side = "SHORT"
            fair_price *= 1.01

        else:
            print(
                "NO DIRECTION"
            )
            return

        fair_price = round(
            fair_price,
            6
        )

        usdt_order = POSITION_USDT

        quantity_full = (
            usdt_order / last_price
        )

        quantity_full = round(
            quantity_full,
            8
        )

        logs_process(
            "ЗАБЛОКИРОВАННЫЕ ПОЗИЦИИ: "
            + str(global_banlist),
            ticker
        )

        logs_process(
            str(time.time())
            + " сайд при открытии "
            + side
            + " || колво при открытии "
            + str(quantity_full)
            + " || текущая цена при открытии "
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
                LEVERAGE,
                quantity_full,
                fair_price,
                last_price,
                percentage,
                current_price
            )

            all_signals.append(
                ticker
            )

        else:
            print(
                "ошибка в хендлере у open_position"
            )
            return

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

    if len(parts) == 1 or parts[1] == "commands":
        await event.reply(
            "команды:\n"
            "/ban list\n"
            "/ban add BTC-USDT\n"
            "/ban del BTC-USDT\n"
            "/ban clear"
        )

    elif parts[1] == "list":
        if global_banlist:
            msg = (
                "Глобальный банлист:\n"
                + "\n".join(
                    sorted(global_banlist)
                )
            )
        else:
            msg = (
                "Глобальный банлист пустой"
            )

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
            f"{symbol} добавлен "
            "в глобальный банлист"
        )

        logs_process(
            symbol
            + " был добавлен "
            + "в GLOBAL BLOCK LIST",
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
                f"{symbol} удалён "
                "из глобального банлиста"
            )

            logs_process(
                symbol
                + " был удален "
                + "из GLOBAL BLOCK LIST",
                symbol,
                True
            )

        else:
            await event.reply(
                f"{symbol} не найден в бане"
            )

    elif parts[1] == "clear":
        global_banlist.clear()

        save_banlist()

        await event.reply(
            "Банлист очищен"
        )


@bot.on(
    events.NewMessage(
        pattern=r"^/(stop|start|restart)"
    )
)
async def pm2_control_handler(event):
    parts = (
        event.message.message
        .strip()
        .split()
    )

    command = parts[0][1:]

    pm2_app_name = os.getenv(
        "PM2_APP_NAME",
        "2"
    )

    if command == "stop":
        result = subprocess.run(
            [
                "pm2",
                "stop",
                pm2_app_name
            ],
            capture_output=True,
            text=True
        )

        await event.reply(
            f"Бот остановлен\n"
            f"```{result.stdout or result.stderr}```"
        )

    elif command == "restart":
        result = subprocess.run(
            [
                "pm2",
                "restart",
                pm2_app_name
            ],
            capture_output=True,
            text=True
        )

        await event.reply(
            f"Бот перезапущен\n"
            f"```{result.stdout or result.stderr}```"
        )

    elif command == "start":
        result = subprocess.run(
            [
                "pm2",
                "start",
                pm2_app_name
            ],
            capture_output=True,
            text=True
        )

        await event.reply(
            f"Бот запущен\n"
            f"```{result.stdout or result.stderr}```"
        )


bot.run_until_disconnected()