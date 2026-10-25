
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

COINBASE_URL = (
    "https://api.exchange.coinbase.com/products/"
    "BTC-USD/candles"
)

YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
    "GC%3DF"
)

)

TIMEFRAME_SECONDS = 900
MIN_CANDLES = 80

# These are signal filters, not a promise of accuracy.
MIN_RSI_BUY = 52
MAX_RSI_BUY = 67
MIN_RSI_SELL = 33
MAX_RSI_SELL = 48
MIN_REWARD_RISK = 1.5


def get_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 SignalBot/2.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


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

    def calc(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100 - 100 / (1 + gain / loss)

    result[period] = calc(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        k = i - 1
        avg_gain = (
            avg_gain * (period - 1) + gains[k]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[k]
        ) / period
        result[i] = calc(avg_gain, avg_loss)

    return result


def fetch_btc():
    params = urllib.parse.urlencode({"granularity": 900})
    data = get_json(f"{COINBASE_URL}?{params}")

    if not isinstance(data, list):
        raise RuntimeError(f"Coinbase response error: {data}")

    now = int(datetime.now(timezone.utc).timestamp())
    candles = sorted(data, key=lambda c: c[0])
    candles = [
        c for c in candles
        if int(c[0]) + TIMEFRAME_SECONDS <= now
    ]

    return [
        {
            "time": int(c[0]),
            "open": float(c[3]),
            "high": float(c[2]),
            "low": float(c[1]),
            "close": float(c[4]),
        }
        for c in candles
    ]


def fetch_gold():
    params = urllib.parse.urlencode({
        "interval": "15m",
        "range": "5d",
    })
    data = get_json(f"{YAHOO_URL}?{params}")

    chart = data.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"Gold data error: {chart['error']}")

    results = chart.get("result") or []
    if not results:
        raise RuntimeError("Gold price source returned no data.")

    item = results[0]
    timestamps = item.get("timestamp") or []
    quote = item.get("indicators", {}).get("quote", [{}])[0]

    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []

    now = int(datetime.now(timezone.utc).timestamp())
    candles = []

    for i, ts in enumerate(timestamps):
        if ts + TIMEFRAME_SECONDS > now:
            continue

        vals = [opens[i], highs[i], lows[i], closes[i]]
        if any(v is None for v in vals):
            continue

        candles.append({
            "time": int(ts),
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
        })

    return candles


def analyze_candles(candles, symbol):
    if len(candles) < MIN_CANDLES:
        raise RuntimeError(
            f"{symbol}: insufficient closed candles ({len(candles)})."
        )

    # Reject the series if the latest bars have a data gap.
    recent = candles[-60:]
    for a, b in zip(recent, recent[1:]):
        if b["time"] - a["time"] != TIMEFRAME_SECONDS:
            raise RuntimeError(
                f"{symbol}: gap in recent candle data; "
                "signal skipped for safety."
            )

    close = [c["close"] for c in candles]
    high = [c["high"] for c in candles]
    low = [c["low"] for c in candles]

    e20 = ema(close, 20)
    e50 = ema(close, 50)
    rsi_values = rsi(close, 14)

    tr = []
    for i in range(len(candles)):
        if i == 0:
            tr.append(high[i] - low[i])
        else:
            tr.append(max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            ))

    atr_values = ema(tr, 14)
    i = len(close) - 1
    prev = i - 1

    if any(x[i] is None for x in (e20, e50, rsi_values, atr_values)):
        return None

    price = close[i]
    atr = atr_values[i]
    r = rsi_values[i]

    if atr <= 0 or price <= 0:
        return None

    candle_range = high[i] - low[i]
    if candle_range <= 0:
        return None

    # Trend, pullback reclaim, momentum and candle confirmation.
    buy = (
        e20[i] > e50[i]
        and close[i] > e20[i]
        and close[prev] <= e20[prev]
        and MIN_RSI_BUY <= r <= MAX_RSI_BUY
        and close[i] >= low[i] + 0.65 * candle_range
    )

    sell = (
        e20[i] < e50[i]
        and close[i] < e20[i]
        and close[prev] >= e20[prev]
        and MIN_RSI_SELL <= r <= MAX_RSI_SELL
        and close[i] <= high[i] - 0.65 * candle_range
    )

    if not buy and not sell:
        return None

    side = "BUY" if buy else "SELL"
    direction = 1 if buy else -1

    # Reference levels only; they are not exchange execution prices.
    entry = price
    stop_distance = max(1.2 * atr, entry * 0.001)
    stop = entry - direction * stop_distance
    target = entry + direction * stop_distance * MIN_REWARD_RISK

    candle_time = datetime.fromtimestamp(
        candles[i]["time"], timezone.utc
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
            "Trend aligned; price reclaimed EMA20; "
            "RSI and candle close confirm momentum."
        ),
    }


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("Telegram secrets are missing.")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
    }).encode("utf-8")

    request = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message.")


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
        "Technical signal only. Verify price and risk before acting."
    )


def main():
    errors = []
    signals = []

    sources = [
        ("BTC-USD", fetch_btc),
        ("XAUUSD", fetch_gold),
    ]

    for symbol, fetcher in sources:
        try:
            candles = fetcher()
            signal = analyze_candles(candles, symbol)
            if signal:
                signals.append(signal)
                print(f"{symbol}: signal found.")
            else:
                print(f"{symbol}: no qualifying signal.")
        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            print(errors[-1])

    # Send only trade signals; do not spam status messages.
    for signal in signals:
        send_telegram(format_signal(signal))

    if errors:
        # Make workflow failures visible in GitHub Actions logs.
        # A source failure must never be interpreted as a no-signal result.
        raise RuntimeError("One or more market sources failed: " + " | ".join(errors))

    print(f"Completed. Signals sent: {len(signals)}")


if __name__ == "__main__":
    main()
