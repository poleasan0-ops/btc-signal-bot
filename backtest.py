
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GRANULARITY = 300
INITIAL_BALANCE = 1000.0
DAYS = 30

# فرض‌های هزینه؛ قبل از استفاده واقعی باید با شرایط صرافی تطبیق داده شوند.
FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002
SPREAD_RATE = 0.0002

# ریسک و هدف بر حسب فاصله قیمت ورود
RISK_FRACTION = 0.005
REWARD_RISK = 1.5


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
        request = urllib.request.Request(
            f"{URL}?{params}",
            headers={"User-Agent": "BTC-Scalp-Backtest/3.0"},
        )

        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        if isinstance(data, dict):
            raise RuntimeError(f"Coinbase API error: {data}")

        for candle in data:
            if candle[0] + GRANULARITY <= end:
                all_candles[candle[0]] = candle

        cursor = chunk_end

    candles = [all_candles[t] for t in sorted(all_candles)]

    # فقط کندل‌های متوالی؛ حذف شکاف‌های داده
    return [
        candle for i, candle in enumerate(candles)
        if i == 0 or candle[0] == candles[i - 1][0] + GRANULARITY
    ]


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

    def value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100 - 100 / (1 + gain / loss)

    result[period] = value(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change_index = i - 1
        avg_gain = (
            avg_gain * (period - 1) + gains[change_index]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[change_index]
        ) / period
        result[i] = value(avg_gain, avg_loss)

    return result


def aggregate_15m(candles):
    groups = {}
    for candle in candles:
        bucket = candle[0] - candle[0] % 900
        groups.setdefault(bucket, []).append(candle)

    result = {}
    for bucket, rows in groups.items():
        rows.sort(key=lambda x: x[0])
        if len(rows) != 3:
            continue
        if [r[0] for r in rows] != [
            bucket, bucket + 300, bucket + 600
        ]:
            continue

        result[bucket] = {
            "open": float(rows[0][3]),
            "high": max(float(r[2]) for r in rows),
            "low": min(float(r[1]) for r in rows),
            "close": float(rows[-1][4]),
        }
    return result


def main():
    candles = get_candles()
    if len(candles) < 500:
        raise RuntimeError(
            f"Not enough continuous 5-minute candles: {len(candles)}"
        )

    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[1]) for c in candles]
    opens = [float(c[3]) for c in candles]
    times = [int(c[0]) for c in candles]

    # 15-minute trend: use only the last fully closed 15-minute bar.
    bars15 = aggregate_15m(candles)
    times15 = sorted(bars15)
    close15 = [bars15[t]["close"] for t in times15]
    trend_fast = ema(close15, 20)
    trend_slow = ema(close15, 50)

    trend_by_time = {}
    for j, t in enumerate(times15):
        if trend_fast[j] is not None and trend_slow[j] is not None:
            trend_by_time[t + 900] = (
                trend_fast[j] > trend_slow[j],
                trend_fast[j] < trend_slow[j],
            )

    fast = ema(closes, 20)
    slow = ema(closes, 50)
    momentum = rsi(closes, 14)

    # ATR(14), using only historical bars.
    tr = []
    for i in range(len(candles)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    atr = ema(tr, 14)

    balance = INITIAL_BALANCE
    peak_equity = balance
    max_drawdown = 0.0
    position = None
    trades = []
    equity_curve = []

    # Enter at next bar open; no same-bar signal execution.
    for i in range(60, len(candles) - 1):
        if fast[i] is None or slow[i] is None:
            continue
        if momentum[i] is None or atr[i] is None or atr[i] <= 0:
            continue

        # Map to the latest fully completed 15m bar.
        bucket = times[i] - times[i] % 900
        trend = trend_by_time.get(bucket)
        if trend is None:
            continue

        uptrend, downtrend = trend
        candle_range = highs[i] - lows[i]
        if candle_range <= 0:
            continue

        # Stronger filters: trend alignment, pullback reclaim,
        # momentum and close near the candle's directional extreme.
        buy = (
            uptrend
            and fast[i] > slow[i]
            and closes[i] > fast[i]
            and closes[i - 1] <= fast[i - 1]
            and 52 <= momentum[i] <= 65
            and closes[i] >= lows[i] + 0.70 * candle_range
        )

        sell = (
            downtrend
            and fast[i] < slow[i]
            and closes[i] < fast[i]
            and closes[i - 1] >= fast[i - 1]
            and 35 <= momentum[i] <= 48
            and closes[i] <= highs[i] - 0.70 * candle_range
        )

        next_open = opens[i + 1]
        timestamp = times[i + 1]

        # Conservative single-position simulation, long and short.
        if position is None and (buy or sell):
            direction = 1 if buy else -1
            entry = next_open * (
                1 + direction * (SPREAD_RATE / 2 + SLIPPAGE_RATE)
            )
            stop_distance = max(atr[i] * 1.2, entry * 0.001)
            risk_cash = balance * RISK_FRACTION
            units = risk_cash / stop_distance

            # Reserve both entry cost and estimated entry fee.
            entry_cost = units * entry
            entry_fee = entry_cost * FEE_RATE
            if entry_cost + entry_fee > balance:
                units = balance / (entry * (1 + FEE_RATE))
                entry_cost = units * entry
                entry_fee = entry_cost * FEE_RATE

            stop = entry - direction * stop_distance
            target = entry + direction * stop_distance * REWARD_RISK

            position = {
                "direction": direction,
                "entry": entry,
                "entry_time": timestamp,
                "units": units,
                "entry_fee": entry_fee,
                "stop": stop,
                "target": target,
                "balance_before": balance,
            }

        elif position is not None:
            direction = position["direction"]

            # Use intrabar high/low for stop and target. If both hit,
            # assume stop first (conservative).
            stop_hit = (
                lows[i + 1] <= position["stop"]
                if direction == 1
                else highs[i + 1] >= position["stop"]
            )
            target_hit = (
                highs[i + 1] >= position["target"]
                if direction == 1
                else lows[i + 1] <= position["target"]
            )

            exit_price = None
            exit_reason = None

            if stop_hit:
                exit_price = position["stop"]
                exit_reason = "STOP"
            elif target_hit:
                exit_price = position["target"]
                exit_reason = "TARGET"
            elif (
                (direction == 1 and (sell or downtrend))
                or (direction == -1 and (buy or uptrend))
            ):
                exit_price = next_open
                exit_reason = "SIGNAL"

            if exit_price is not None:
                exit_price *= (
                    1 - direction * (SPREAD_RATE / 2 + SLIPPAGE_RATE)
                )
                gross_pnl = (
                    (exit_price - position["entry"])
                    * position["units"] * direction
                )
                exit_fee = (
                    exit_price * position["units"] * FEE_RATE
                )
                pnl = gross_pnl - exit_fee - position["entry_fee"]
                balance += pnl

                trades.append({
                    "pnl": pnl,
                    "reason": exit_reason,
                })
                position = None

        mark = closes[i + 1]
        equity = balance
        if position is not None:
            unrealized = (
                (mark - position["entry"])
                * position["units"] * position["direction"]
                - position["entry_fee"]
            )
            equity = balance + unrealized

        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            max_drawdown = max(
                max_drawdown, (peak_equity - equity) / peak_equity
            )
        equity_curve.append(equity)

    final_equity = balance
    if position is not None:
        final_equity += (
            (closes[-1] - position["entry"])
            * position["units"] * position["direction"]
            - position["entry_fee"]
        )

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_wins = sum(t["pnl"] for t in wins)
    gross_losses = -sum(t["pnl"] for t in losses)
    profit_factor = (
        gross_wins / gross_losses if gross_losses > 0 else None
    )

    print("BTC-USD 5m/15m experimental scalp backtest")
    print(f"Candles: {len(candles)}")
    print(f"Start: {datetime.fromtimestamp(times[0], timezone.utc)}")
    print(f"End:   {datetime.fromtimestamp(times[-1], timezone.utc)}")
    print(f"Initial balance: ${INITIAL_BALANCE:.2f}")
    print(f"Closed balance: ${balance:.2f}")
    print(f"Final equity including open P/L: ${final_equity:.2f}")
    print(
        f"Equity return: "
        f"{(final_equity / INITIAL_BALANCE - 1) * 100:.2f}%"
    )
    print(f"Closed trades: {len(trades)}")
    print(f"Wins: {len(wins)}")
    print(f"Losses: {len(losses)}")
    print(
        f"Win rate: {len(wins) / len(trades) * 100:.2f}%"
        if trades else "Win rate: N/A"
    )
    print(
        f"Profit factor: {profit_factor:.2f}"
        if profit_factor is not None else "Profit factor: N/A"
    )
    print(f"Maximum equity drawdown: {max_drawdown * 100:.2f}%")
    if position:
        print("Open position at end: Yes (unrealized P/L included in equity)")
    else:
        print("Open position at end: None")
    print(
        "Experimental only: costs are assumptions; results do not "
        "guarantee future performance."
    )


if __name__ == "__main__":
    main()
