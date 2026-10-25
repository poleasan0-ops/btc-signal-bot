import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# =========================
# Market data sources
# =========================

COINBASE_URL = (
    "https://api.exchange.coinbase.com/products/"
    "BTC-USD/candles"
)

# GC=F is a US gold futures symbol, not spot XAUUSD.
YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF"
)

TIMEFRAME_SECONDS = 900
MIN_CANDLES = 80

MIN_RSI_BUY = 52
MAX_RSI_BUY = 67
MIN_RSI_SELL = 33
MAX_RSI_SELL = 48
MIN_REWARD_RISK = 1.5


# =========================
# Download JSON
# =========================

def get_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 SignalBot/2.0"},
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


# =========================
# Indicators
# =========================

def ema(values, period):
    result = [None] * len(values)

    if len(values) < period:
        return result

    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2 / (period + 1)

    for i in range(period, len(values)):
        current = alpha * values[i] + (1 - alpha) * current
        result[i] = current

    return result


def rsi(values, period=14):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calculate(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0

        return 100 - 100 / (1 + gain / loss)

    result[period] = calculate(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        k = i - 1

        avg_gain = (
            avg_gain * (period - 1) + gains[k]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[k]
        ) / period

        result[i] = calculate(avg_gain, avg_loss)

    return result


# =========================
# Fetch BTC candles
# =========================

def fetch_btc():
    params = urllib.parse.urlencode({
        "granularity": TIMEFRAME_SECONDS
    })

    data = get_json(f"{COINBASE_URL}?{params}")

    if not isinstance(data, list):
        raise RuntimeError(f"Coinbase response error: {data}")

    now = int(datetime.now(timezone.utc).timestamp())

    candles = sorted(data, key=lambda c: c[0])

    result = []

    for c in candles:
        candle_time = int(c[0])

        # Ignore the currently forming candle.
        if candle_time + TIMEFRAME_SECONDS > now:
            continue

        result.append({
            "time": candle_time,
            "open": float(c[3]),
            "high": float(c[2]),
            "low": float(c[1]),
            "close": float(c[4]),
        })

    return result


# =========================
# Fetch gold futures candles
# =========================

def fetch_gold():
    params = urllib.parse.urlencode({
        "interval": "15m",
        "range": "5d",
    })

    data = get_json(f"{YAHOO_URL}?{params}")

    chart = data.get("chart", {})

    if chart.get("error"):
        raise RuntimeError(
            f"Yahoo gold data error: {chart['error']}"
        )

    results = chart.get("result") or []

    if not results:
        raise RuntimeError(
            "Yahoo returned no gold futures data."
        )

    item = results[0]

    timestamps = item.get("timestamp") or []

    quote_list = item.get("indicators", {}).get("quote", [{}])
    quote = quote_list[0] if quote_list else {}

    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []

    now = int(datetime.now(timezone.utc).timestamp())
    candles = []

    for i, ts in enumerate(timestamps):
        ts = int(ts)

        # Ignore the currently forming candle.
        if ts + TIMEFRAME_SECONDS > now:
            continue

        if i >= len(opens) or i >= len(highs):
            continue

        if i >= len(lows) or i >= len(closes):
            continue

        values = [opens[i], highs[i], lows[i], closes[i]]

        if any(v is None for v in values):
            continue

        candles.append({
            "time": ts,
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
        })

    return sorted(candles, key=lambda c: c["time"])


# =========================
# Analyze candles
# =========================

def analyze_candles(candles, symbol, allow_session_gaps=False):
    if len(candles) < MIN_CANDLES:
        raise RuntimeError(
            f"{symbol}: insufficient closed candles "
            f"({len(candles)})."
        )

    # Crypto trades continuously, so recent gaps are suspicious.
    # Gold futures have market-session breaks, so gaps are allowed.
    if not allow_session_gaps:
        recent = candles[-60:]

        for a, b in zip(recent, recent[1:]):
            if b["time"] - a["time"] != TIMEFRAME_SECONDS:
                raise RuntimeError(
                    f"{symbol}: gap in recent candle data; "
                    "signal skipped."
                )

    close = [c["close"] for c in candles]
    high = [c["high"] for c in candles]
    low = [c["low"] for c in candles]

    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    rsi_values = rsi(close, 14)

    true_ranges = []

    for i in range(len(candles)):
        if i == 0:
            true_ranges.append(high[i] - low[i])
        else:
            true_ranges.append(
                max(
                    high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]),
                )
            )

    atr_values = ema(true_ranges, 14)

    i = len(close) - 1
    prev = i - 1

    if any(
        values[i] is None or values[prev] is None
        for values in (ema20, ema50, rsi_values, atr_values)
    ):
        return None

    price = close[i]
    atr = atr_values[i]
    r = rsi_values[i]

    if atr <= 0 or price <= 0:
        return None

    candle_range = high[i] - low[i]

    if candle_range <= 0:
        return None

    # BUY setup
    buy = (
        ema20[i] > ema50[i]
        and close[i] > ema20[i]
        and close[prev] <= ema20[prev]
        and MIN_RSI_BUY <= r <= MAX_RSI_BUY
        and close[i] >= low[i] + 0.65 * candle_range
    )

    # SELL setup
    sell = (
        ema20[i] < ema50[i]
        and close[i] < ema20[i]
        and close[prev] >= ema20[prev]
        and MIN_RSI_SELL <= r <= MAX_RSI_SELL
        and close[i] <= high[i] - 0.65 * candle_range
    )

    if not buy and not sell:
        return None

    side = "BUY" if buy else "SELL"
    direction = 1 if buy else -1

    entry = price
    stop_distance = max(1.2 * atr, entry * 0.001)

    stop = entry - direction * stop_distance

    target = (
        entry + direction * stop_distance * MIN_REWARD_RISK
    )

    candle_time = datetime.fromtimestamp(
        candles[i]["time"],
        timezone.utc,
    ).strftime("%Y-%m-%d %H:%M UTC")

    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rsi": r,
        "atr": atr,
        "time": candle_time,
        "reason": (
            "EMA trend alignment, EMA20 reclaim, "
            "RSI filter and candle confirmation."
        ),
    }


# =========================
# Send Telegram message
# =========================

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Telegram secrets are missing from GitHub."
        )

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
    }).encode("utf-8")

    request = urllib.request.Request(url, data=payload)

    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram rejected the message: {result}"
        )


# =========================
# Format signal
# =========================

def format_signal(signal):
    return (
        f"{signal['symbol']} | {signal['side']}\n"
        f"Timeframe: 15m\n"
        f"Entry reference: {signal['entry']:.4f}\n"
        f"Stop-loss reference: {signal['stop']:.4f}\n"
        f"Take-profit reference: {signal['target']:.4f}\n"
        f"RSI: {signal['rsi']:.1f}\n"
        f"ATR: {signal['atr']:.4f}\n"
        f"Candle: {signal['time']}\n"
        f"Reason: {signal['reason']}\n\n"
        "Technical signal only. Verify current price and risk."
    )


# =========================
# Main
# =========================

def main():
    sources = [
        ("BTC-USD", fetch_btc, False),
        ("XAUUSD (GC=F futures)", fetch_gold, True),
    ]

    errors = []
    signals = []

    for symbol, fetcher, allow_gaps in sources:
        try:
            candles = fetcher()

            print(
                f"{symbol}: downloaded {len(candles)} candles."
            )

            signal = analyze_candles(
                candles,
                symbol,
                allow_session_gaps=allow_gaps,
            )

            if signal:
                signals.append(signal)
                print(f"{symbol}: qualifying signal found.")
            else:
                print(f"{symbol}: no qualifying signal.")

        except Exception as exc:
            error = (
                f"{symbol}: {type(exc).__name__}: {exc}"
            )
            errors.append(error)
            print(error)

    # Send signals from sources that worked.
    for signal in signals:
        try:
            send_telegram(format_signal(signal))
            print(f"{signal['symbol']}: Telegram message sent.")
        except Exception as exc:
            errors.append(
                f"Telegram: {type(exc).__name__}: {exc}"
            )
            print(errors[-1])

    if errors:
        raise RuntimeError(
            "Some operations failed: " + " | ".join(errors)
        )

    print(f"Completed. Signals sent: {len(signals)}")


if __name__ == "__main__":
    main()
