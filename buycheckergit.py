import os
import time
from decimal import Decimal

import pandas as pd
import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine

load_dotenv()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
BEFORE_PUMP_API_KEY = os.getenv("PUMP_DATE")
ANALYTICS_WALLET_API_KEY = os.getenv("ANALYTICS_WALLET")
ENGINE_SQL = os.getenv("ENGINE_SQL")

token_mint = input("write ur token mint ")

DEXSCREENER_URL = os.getenv(
    "DEXSCREENER_URL",
    "https://api.dexscreener.com/token-pairs/v1/solana/{token_mint}"
)

WALLETS_CSV = os.getenv("WALLETS_CSV", "wallets_db.csv")
WALLETS_SQL_TABLE = os.getenv("WALLETS_SQL_TABLE", "wallet_db_sql")

required_env = {
    "HELIUS_API_KEY": HELIUS_API_KEY,
    "PUMP_DATE": BEFORE_PUMP_API_KEY,
    "ENGINE_SQL": ENGINE_SQL,
}

missing_env = [name for name, value in required_env.items() if not value]

if missing_env:
    raise RuntimeError(
        f"Missing environment variables: {', '.join(missing_env)}"
    )

url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

signature_received = False
_system_wallet_cache = {}
_symbol_cache = {
    "So11111111111111111111111111111111111111112": "SOL"
}

engine = create_engine(ENGINE_SQL)


def get_time_started(mint: str):
    response = requests.get(
        DEXSCREENER_URL,
        headers={"Accept": "*/*"},
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    created_at = data[0]["pairCreatedAt"]

    return created_at // 1000


def get_time_pump(mint: str):
    before_pump_url = f"https://data.solanatracker.io/chart/{mint}"
    now_time = int(time.time())
    start_existence = get_time_started(mint)

    candles_data = pd.DataFrame(columns=["Timestamp", "Price"])

    response = requests.get(
        before_pump_url,
        headers={
            "x-api-key": BEFORE_PUMP_API_KEY
        },
        params={
            "type": "4h",
            "time_from": start_existence,
            "time_to": now_time,
            "currency": "usd"
        },
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

    candles = data["oclhv"]

    for candle in candles:
        candles_data.loc[len(candles_data)] = [
            int(candle["time"]),
            candle["close"]
        ]

    candles_data["changed_price"] = (
        candles_data["Price"] - candles_data["Price"].shift(1)
    )

    candles_data["percentage_changing"] = (
        (
            (candles_data["Price"] - candles_data["Price"].shift(1))
            / candles_data["Price"].shift(1)
            * 100
        ).round(2)
    )

    candles_data["Timestamp"] = candles_data["Timestamp"].astype("int64")

    quantile_95 = candles_data["percentage_changing"].quantile(0.95)

    top_5percentage = (
        candles_data
        .query("percentage_changing >= @quantile_95")
        .reset_index(drop=True)
    )

    mean_changing = top_5percentage["percentage_changing"].mean()
    start_analytics = 0

    for price in candles_data["percentage_changing"]:
        if price > mean_changing:
            start_analytics = candles_data.loc[
                candles_data["percentage_changing"] == price,
                "Timestamp"
            ]
            break

    return [start_existence, start_analytics, mean_changing]


def get_symbol(mint: str) -> str:
    if mint in _symbol_cache:
        return _symbol_cache[mint]

    try:
        response = requests.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": "symbol",
                "method": "getAsset",
                "params": {"id": mint},
            },
            timeout=30,
        )

        response.raise_for_status()

        symbol = response.json()["result"]["token_info"]["symbol"]

    except Exception:
        symbol = mint[:6]

    _symbol_cache[mint] = symbol

    return symbol


def is_system_wallet(pubkey: str) -> bool:
    if pubkey in _system_wallet_cache:
        return _system_wallet_cache[pubkey]

    try:
        response = requests.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [
                    pubkey,
                    {
                        "encoding": "base64"
                    }
                ]
            },
            timeout=30,
        )

        response.raise_for_status()

        result = response.json().get("result", {}).get("value")

        is_wallet = (
            True
            if result is None
            else result.get("owner")
            == "11111111111111111111111111111111"
        )

    except Exception as e:
        print(f"Ошибка проверки владельца {pubkey}: {e}")
        is_wallet = False

    _system_wallet_cache[pubkey] = is_wallet

    return is_wallet


def balance_by_mint(balances, mint, owner):
    total = Decimal(0)

    for item in balances:
        if item.get("owner") != owner or item.get("mint") != mint:
            continue

        amount = Decimal(item["uiTokenAmount"]["amount"])
        decimals = item["uiTokenAmount"]["decimals"]

        total += amount / (Decimal(10) ** decimals)

    return total


def signature_info(signature: str):
    global signature_received

    response_sign = None

    for _ in range(5):
        response = requests.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "finalized",
                        "maxSupportedTransactionVersion": 0
                    }
                ]
            },
            timeout=30,
        )

        response.raise_for_status()

        response_sign = response.json()

        if response_sign.get("result") is not None:
            if not signature_received:
                print("Успешное получение signature")
                signature_received = True

            break

        time.sleep(3)

    if response_sign is None or response_sign.get("result") is None:
        print(
            "Не удалось получить транзакцию после 5 попыток:",
            signature
        )
        return None

    result = response_sign["result"]
    meta = result["meta"]
    account_keys = result["transaction"]["message"]["accountKeys"]

    pre_token_balances = meta.get("preTokenBalances", [])
    post_token_balances = meta.get("postTokenBalances", [])
    pre_balances = meta.get("preBalances", [])
    post_balances = meta.get("postBalances", [])

    trader_wallet = None
    trader_index = None

    for idx, acc in enumerate(account_keys):
        if acc.get("signer"):
            trader_wallet = acc["pubkey"]
            trader_index = idx
            break

    if trader_wallet is None:
        return None

    if not is_system_wallet(trader_wallet):
        return None

    target_delta = (
        balance_by_mint(
            post_token_balances,
            token_mint,
            trader_wallet
        )
        - balance_by_mint(
            pre_token_balances,
            token_mint,
            trader_wallet
        )
    )

    if target_delta == 0:
        return None

    other_mints = {
        item["mint"]
        for item in pre_token_balances + post_token_balances
        if (
            item.get("owner") == trader_wallet
            and item.get("mint") != token_mint
        )
    }

    quote_candidates = []

    for mint in other_mints:
        delta = (
            balance_by_mint(
                post_token_balances,
                mint,
                trader_wallet
            )
            - balance_by_mint(
                pre_token_balances,
                mint,
                trader_wallet
            )
        )

        if delta != 0 and (target_delta > 0) != (delta > 0):
            quote_candidates.append((mint, delta))

    if (
        trader_index is not None
        and trader_index < len(pre_balances)
        and trader_index < len(post_balances)
    ):
        sol_delta = Decimal(
            post_balances[trader_index] - pre_balances[trader_index]
        ) / Decimal(10 ** 9)

        if sol_delta != 0 and (target_delta > 0) != (sol_delta > 0):
            quote_candidates.append(("SOL_NATIVE", sol_delta))

    if not quote_candidates:
        return None

    quote_candidates.sort(
        key=lambda candidate: abs(candidate[1]),
        reverse=True
    )

    quote_mint, quote_delta = quote_candidates[0]

    quote_symbol = (
        "SOL"
        if quote_mint == "SOL_NATIVE"
        else get_symbol(quote_mint)
    )

    purchase = "BUY" if target_delta > 0 else "SELL"

    return [
        purchase,
        trader_wallet,
        abs(target_delta),
        quote_symbol,
        abs(quote_delta)
    ]


def fetch_transactions_range(mint: str, limit: int = 100):
    symbol = get_symbol(mint)

    start_existence, time_pump, max_pump = get_time_pump(mint)

    counter = 0
    before_signature = None

    while counter <= 10:
        counter += 1

        params = {
            "api-key": HELIUS_API_KEY,
            "limit": limit,
            "type": "SWAP",
            "gte-time": start_existence,
            "lte-time": time_pump,
        }

        if before_signature:
            params["before-signature"] = before_signature

        response = requests.get(
            f"https://api.helius.xyz/v0/addresses/{mint}/transactions",
            params=params,
            timeout=30,
        )

        response.raise_for_status()

        transactions = response.json()

        if not transactions:
            break

        for transaction in transactions:
            signature = transaction.get("signature")
            pool_source = transaction.get("source", "UNKNOWN")

            transaction_info = signature_info(signature)

            if transaction_info is None:
                continue

            (
                purchase,
                trader_wallet,
                quantity,
                quote_symbol,
                quote_amount
            ) = transaction_info

            if purchase == "BUY":
                row = pd.DataFrame([{
                    "Timestamp": transaction.get("timestamp"),
                    "Time": pd.to_datetime(
                        transaction.get("timestamp"),
                        unit="s",
                        utc=True
                    ).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "Symbol": symbol,
                    "Action": purchase,
                    "Quantity": float(quantity),
                    "Wallet": trader_wallet,
                    "Price": f"{float(quote_amount)} {quote_symbol}",
                    "Pool": pool_source,
                }])

                row.to_csv(
                    WALLETS_CSV,
                    mode="a",
                    header=not os.path.exists(WALLETS_CSV),
                    index=False,
                    encoding="utf-8-sig"
                )

                row.to_sql(
                    WALLETS_SQL_TABLE,
                    con=engine,
                    if_exists="append",
                    index=False
                )

        before_signature = transactions[-1]["signature"]

        if len(transactions) < limit:
            break


fetch_transactions_range(
    token_mint,
    limit=100
)
