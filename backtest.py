
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

COINBASE_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GRANULARITY = 300  # 5-minute candles


def get_candles():
    query = urllib.parse.urlencode({"granularity": GRANULARITY})
    request = urllib.request.Request(
        f"{COINBASE_URL}?{query}",
        headers={"User-Agent": "BTC-Signal-Backtest/1.0"},
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        candles = json.loads(response.read().decode("utf-8"))

    candles.sort(key=lambda candle: candle[0])

    now = int(datetime.now(timezone.utc).timestamp())
    candles = [
        candle for candle in candles
        if int(candle[0]) + GRANULARITY <= now
    ]

    if not candles:
        raise RuntimeError("No closed candles received.")

    return candles


def main():
    candles = get_candles()
    first = datetime.fromtimestamp(
        candles[0][0], timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")
    last = datetime.fromtimestamp(
        candles[-1][0], timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    print("BTC-USD historical data check")
    print(f"Closed candles received: {len(candles)}")
    print(f"First candle: {first}")
    print(f"Last candle: {last}")
    print("This is a data check, not a profitability backtest.")


if __name__ == "__main__":
    main()
