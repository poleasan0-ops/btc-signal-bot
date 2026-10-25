import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GRANULARITY = 300
FEE_RATE = 0.0005  # فرض کارمزد 0.05% در هر سمت
INITIAL_BALANCE = 1000.0


def get_candles():
    all_candles = {}
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - 30 * 24 * 60 * 60

    while start < end:
        chunk_end = min(start + 300 * GRANULARITY, end)
        params = urllib.parse.urlencode({
            "granularity": GRANULARITY,
            "start": datetime.fromtimestamp(
                start, timezone.utc
            ).isoformat(),
            "end": datetime.fromtimestamp(
                chunk_end, timezone.utc
            ).isoformat(),
        })
        req = urllib.request.Request(
            f"{URL}?{params}",
            headers={"User-Agent": "BTC-Backtest/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        for c in data:
            if c[0] + GRANULARITY <= end:
                all_candles[c[0]] = c

        start = chunk_end
    return [all_candles[t] for t in sorted(all_candles)]


def ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result

    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2 / (period + 1)

    for i in range(period, len(values)):
        current = values[i] * alpha + current * (1 - alpha)
        result[i] = current
    return result


def rsi(values, period=14):
    result = [None] * len(values)
    if len(values) <= period:
        return result

    gains = [
        max(values[i] - values[i - 1], 0)
        for i in range(1, period + 1)
    ]
    losses = [
        max(values[i - 1] - values[i], 0)
        for i in range(1, period + 1)
    ]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def calc(g, l):
        if l == 0:
            return 100.0
        return 100 - 100 / (1 + g / l)

    result[period] = calc(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
        result[i] = calc(avg_gain, avg_loss)

    return result


def main():
    candles = get_candles()
    if len(candles) < 100:
        raise RuntimeError(f"Not enough candles: {len(candles)}")

    closes = [float(c[4]) for c in candles]
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    macd_fast = ema(closes, 12)
    macd_slow = ema(closes, 26)

    macd_line = [
        (macd_fast[i] - macd_slow[i])
        if macd_fast[i] is not None and macd_slow[i] is not None
        else None
        for i in range(len(closes))
    ]
    macd_signal = ema(
        [x if x is not None else 0.0 for x in macd_line], 9
    )
    rsi_values = rsi(closes)

    balance = INITIAL_BALANCE
    peak = balance
    max_drawdown = 0.0
    position = None
    trades = []

    for i in range(60, len(closes) - 1):
        if any(x is None for x in (
            fast[i], slow[i], macd_line[i],
            macd_signal[i], rsi_values[i]
        )):
            continue

        buy = (
            fast[i] > slow[i]
            and macd_line[i] > macd_signal[i]
            and 50 < rsi_values[i] < 70
        )
        sell = (
            fast[i] < slow[i]
            and macd_line[i] < macd_signal[i]
            and 30 < rsi_values[i] < 50
        )

        # ورود/خروج در قیمت بازشدن کندل بعدی
        price = float(candles[i + 1][3])

        if position is None and buy:
            position = price
            balance *= (1 - FEE_RATE)
        elif position is not None and sell:
            gross_return = price / position
            balance *= gross_return * (1 - FEE_RATE)
            pnl = balance - trades[-1]["balance_before"]
            trades[-1]["exit"] = price
            trades[-1]["pnl"] = pnl
            trades[-1]["result"] = "WIN" if pnl > 0 else "LOSS"
            position = None

        if position is not None and not trades or (
            position is not None and trades[-1].get("exit") is not None
        ):
            trades.append({
                "entry": position,
                "balance_before": balance,
                "pnl": None,
                "result": "OPEN",
            })

        peak = max(peak, balance)
        if peak > 0:
            max_drawdown = max(
                max_drawdown, (peak - balance) / peak
            )

    closed = [t for t in trades if t["result"] in ("WIN", "LOSS")]
    wins = sum(t["result"] == "WIN" for t in closed)
    net_return = (balance / INITIAL_BALANCE - 1) * 100
    profit_factor = None
    gross_wins = sum(max(t["pnl"], 0) for t in closed)
    gross_losses = -sum(min(t["pnl"], 0) for t in closed)
    if gross_losses > 0:
        profit_factor = gross_wins / gross_losses

    print("BTC-USD 5-minute preliminary backtest")
    print(f"Candles: {len(candles)}")
    print("Period: approximately 30 days (API availability permitting)")
    print(f"Start: {datetime.fromtimestamp(candles[0][0], timezone.utc)}")
    print(f"End:   {datetime.fromtimestamp(candles[-1][0], timezone.utc)}")
    print(f"Initial balance: ${INITIAL_BALANCE:.2f}")
    print(f"Final balance (closed trades): ${balance:.2f}")
    print(f"Net return: {net_return:.2f}%")
    print(f"Closed trades: {len(closed)}")
    print(f"Win rate: {wins / len(closed) * 100:.2f}%" if closed else "Win rate: N/A")
    print(f"Profit factor: {profit_factor:.2f}" if profit_factor is not None else "Profit factor: N/A")
    print(f"Max drawdown (balance only): {max_drawdown * 100:.2f}%")
    print("Caution: preliminary test; spread, slippage, and open-position mark-to-market are not fully modeled.")


if __name__ == "__main__":
    main()
