
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone


BINANCE_URL = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "15m"


def get_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "BTC-Signal-Bot/1.0"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def ema(values, period):
    result = []
    alpha = 2 / (period + 1)
    current = values[0]
    result.append(current)

    for value in values[1:]:
        current = alpha * value + (1 - alpha) * current
        result.append(current)

    return result


def rsi(values, period=14):
    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = [50.0] * period

    def calculate(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100 - (100 / (1 + gain / loss))

    result.append(calculate(avg_gain, avg_loss))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result.append(calculate(avg_gain, avg_loss))

    return result


def get_candles():
    query = urllib.parse.urlencode({
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "limit": 200
    })

    candles = get_json(f"{BINANCE_URL}?{query}")

    # Ignore the latest candle because it may still be forming.
    candles = candles[:-1]

    return {
        "time": [c[0] for c in candles],
        "open": [float(c[1]) for c in candles],
        "high": [float(c[2]) for c in candles],
        "low": [float(c[3]) for c in candles],
        "close": [float(c[4]) for c in candles],
    }


def analyze():
    data = get_candles()
    close = data["close"]
    high = data["high"]
    low = data["low"]

    ema20 = ema(close, 20)
    ema50 = ema(close, 50)

    fast = ema(close, 12)
    slow = ema(close, 26)
    macd = [a - b for a, b in zip(fast, slow)]
    macd_signal = ema(macd, 9)
    rsi_values = rsi(close, 14)

    i = len(close) - 1
    previous = i - 1

    # Average True Range for volatility-based risk levels.
    true_ranges = []
    for j in range(1, len(close)):
        true_ranges.append(max(
            high[j] - low[j],
            abs(high[j] - close[j - 1]),
            abs(low[j] - close[j - 1])
        ))

    atr = sum(true_ranges[-14:]) / 14
    price = close[i]

    crossed_up = (
        ema20[previous] <= ema50[previous]
        and ema20[i] > ema50[i]
    )
    crossed_down = (
        ema20[previous] >= ema50[previous]
        and ema20[i] < ema50[i]
    )

    bullish = (
        crossed_up
        and macd[i] > macd_signal[i]
        and 50 < rsi_values[i] < 68
    )
    bearish = (
        crossed_down
        and macd[i] < macd_signal[i]
        and 32 < rsi_values[i] < 50
    )

    candle_time = datetime.fromtimestamp(
        data["time"][i] / 1000, timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    if bullish:
        return {
            "side": "BUY",
            "price": price,
            "stop": price - 1.5 * atr,
            "target": price + 3 * atr,
            "rsi": rsi_values[i],
            "time": candle_time,
        }

    if bearish:
        return {
            "side": "SELL",
            "price": price,
            "stop": price + 1.5 * atr,
            "target": price - 3 * atr,
            "rsi": rsi_values[i],
            "time": candle_time,
        }

    print(
        f"No signal | BTCUSDT | {candle_time} | "
        f"Price: {price:.2f} | RSI: {rsi_values[i]:.1f}"
    )
    return None


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret."
        )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message
    }).encode("utf-8")

    request = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message.")


def main():
    signal = analyze()

    if signal is None:
        return

    message = (
        f"BTCUSDT | {signal['side']}\n"
        f"Timeframe: 15m\n"
        f"Entry reference: {signal['price']:.2f}\n"
        f"Stop-loss reference: {signal['stop']:.2f}\n"
        f"Take-profit reference: {signal['target']:.2f}\n"
        f"RSI: {signal['rsi']:.1f}\n"
        f"Candle: {signal['time']}\n\n"
        "Rule-based technical signal, not a guarantee."
    )

    send_telegram(message)
    print("Signal sent to Telegram.")


if __name__ == "__main__":
    main()

