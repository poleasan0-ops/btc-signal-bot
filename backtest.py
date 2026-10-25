import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GRANULARITY = 300
FEE_RATE = 0.0005  # فرض کارمزد 0.05% برای هر سمت
INITIAL_BALANCE = 1000.0
DAYS = 30


def get_candles():
    all_candles = {}
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - DAYS * 86400
    cursor = start

    while cursor < end:
        chunk_end = min(cursor + 300 * GRANULARITY, end)
        params = urllib.parse.urlencode({
            "granularity": GRANULARITY,
            "start": datetime.fromtimestamp(
                cursor, timezone.utc
            ).isoformat(),
            "end": datetime.fromtimestamp(
                chunk_end, timezone.utc
            ).isoformat(),
        })
        req = urllib.request.Request(
            f"{URL}?{params}",
            headers={"User-Agent": "BTC-Backtest/2.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        for candle in data:
            if candle[0] + GRANULARITY <= end:
                all_candles[candle[0]] = candle

        cursor = chunk_end

    return [all_candles[t] for t in sorted(all_candles)]


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
    avg_gain = sum(max(x, 0) for x in changes[:period]) / period
    avg_loss = sum(max(-x, 0) for x in changes[:period]) / period

    def calculate(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100 - 100 / (1 + gain / loss)

    result[period] = calculate(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = changes[i - 1]
        avg_gain = (
            avg_gain * (period - 1) + max(change, 0)
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + max(-change, 0)
        ) / period
        result[i] = calculate(avg_gain, avg_loss)

    return result


def main():
    candles = get_candles()
    if len(candles) < 100:
        raise RuntimeError(f"Not enough candles: {len(candles)}")

    closes = [float(c[4]) for c in candles]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)

    macd = [
        ema12[i] - ema26[i]
        if ema12[i] is not None and ema26[i] is not None
        else None
        for i in range(len(closes))
    ]
    macd_signal = ema(
        [value if value is not None else 0.0 for value in macd], 9
    )
    rsi_values = rsi(closes)

    balance = INITIAL_BALANCE
    peak_equity = INITIAL_BALANCE
    max_drawdown = 0.0
    position = None
    trades = []

    for i in range(60, len(candles) - 1):
        if any(value is None for value in (
            ema20[i], ema50[i], macd[i],
            macd_signal[i], rsi_values[i]
        )):
            continue

        buy = (
            ema20[i] > ema50[i]
            and macd[i] > macd_signal[i]
            and 50 < rsi_values[i] < 70
        )
        sell = (
            ema20[i] < ema50[i]
            and macd[i] < macd_signal[i]
            and 30 < rsi_values[i] < 50
        )

        # سیگنال کندل بسته‌شده؛ اجرا در بازشدن کندل بعدی
        price = float(candles[i + 1][3])
        timestamp = candles[i + 1][0]

        if position is None and buy:
            entry_fee = balance * FEE_RATE
            position = {
                "entry": price,
                "entry_time": timestamp,
                "units": (balance - entry_fee) / price,
                "balance_before": balance,
            }
        elif position is not None and sell:
            gross_value = position["units"] * price
            exit_fee = gross_value * FEE_RATE
            balance = gross_value - exit_fee

            pnl = balance - position["balance_before"]
            trades.append({
                "entry": position["entry"],
                "exit": price,
                "pnl": pnl,
                "result": "WIN" if pnl > 0 else "LOSS",
            })
            position = None

        equity = balance
        if position is not None:
            equity = position["units"] * float(candles[i + 1][4])

        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            max_drawdown = max(
                max_drawdown,
                (peak_equity - equity) / peak_equity
            )

    open_pnl = 0.0
    if position is not None:
        last_price = closes[-1]
        estimated_value = position["units"] * last_price * (1 - FEE_RATE)
        open_pnl = estimated_value - position["balance_before"]

    wins = sum(t["result"] == "WIN" for t in trades)
    losses = sum(t["result"] == "LOSS" for t in trades)
    gross_wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_losses = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    profit_factor = (
        gross_wins / gross_losses if gross_losses > 0 else None
    )

    closed_balance = balance
    net_return = (closed_balance / INITIAL_BALANCE - 1) * 100

    print("BTC-USD corrected preliminary backtest")
    print(f"Candles: {len(candles)}")
    print(f"Start: {datetime.fromtimestamp(candles[0][0], timezone.utc)}")
    print(f"End:   {datetime.fromtimestamp(candles[-1][0], timezone.utc)}")
    print(f"Initial balance: ${INITIAL_BALANCE:.2f}")
    print(f"Balance after closed trades: ${closed_balance:.2f}")
    print(f"Net return on closed balance: {net_return:.2f}%")
    print(f"Closed trades: {len(trades)}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    print(
        f"Win rate: {wins / len(trades) * 100:.2f}%"
        if trades else "Win rate: N/A"
    )
    print(
        f"Profit factor: {profit_factor:.2f}"
        if profit_factor is not None else "Profit factor: N/A"
    )
    print(f"Maximum marked-to-market drawdown: {max_drawdown * 100:.2f}%")
    if position is not None:
        print(f"Open position entry: ${position['entry']:.2f}")
        print(f"Estimated open P/L after exit fee: ${open_pnl:.2f}")
    else:
        print("Open position: None")
    print("Fees are estimates; spread and slippage are not included.")


if __name__ == "__main__":
    main()
