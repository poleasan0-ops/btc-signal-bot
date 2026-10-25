
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from collections import defaultdict

URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

GRANULARITY = 300
INITIAL_BALANCE = 1000.0
DAYS = 30

FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002
SPREAD_RATE = 0.0002

RISK_FRACTION = 0.005
REWARD_RISK = 1.5
MAX_NOTIONAL_FRACTION = 1.0


def get_candles():
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - DAYS * 86400
    all_candles = {}
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
            headers={"User-Agent": "BTC-Backtest/4.0"},
        )

        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        if not isinstance(data, list):
            raise RuntimeError(f"API response error: {data}")

        for c in data:
            if len(c) >= 6:
                ts = int(c[0])
                if start <= ts and ts + GRANULARITY <= end:
                    all_candles[ts] = [
                        ts, float(c[1]), float(c[2]),
                        float(c[3]), float(c[4]), float(c[5])
                    ]

        cursor = chunk_end

    raw = [all_candles[t] for t in sorted(all_candles)]

    # Split data into continuous segments. Indicators never cross gaps.
    segments = []
    current = []

    for c in raw:
        if current and c[0] != current[-1][0] + GRANULARITY:
            if current:
                segments.append(current)
            current = []
        current.append(c)

    if current:
        segments.append(current)

    return segments


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out

    value = sum(values[:period]) / period
    out[period - 1] = value
    alpha = 2 / (period + 1)

    for i in range(period, len(values)):
        value = alpha * values[i] + (1 - alpha) * value
        out[i] = value

    return out


def rsi(values, period=14):
    out = [None] * len(values)
    if len(values) <= period:
        return out

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calc(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[period] = calc(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        k = i - 1
        avg_gain = (avg_gain * (period - 1) + gains[k]) / period
        avg_loss = (avg_loss * (period - 1) + losses[k]) / period
        out[i] = calc(avg_gain, avg_loss)

    return out


def build_15m(segment):
    groups = defaultdict(list)

    for c in segment:
        bucket = c[0] - c[0] % 900
        groups[bucket].append(c)

    bars = {}

    for bucket, rows in groups.items():
        rows.sort(key=lambda x: x[0])
        expected = [bucket, bucket + 300, bucket + 600]

        if [r[0] for r in rows] != expected:
            continue

        bars[bucket] = {
            "close": rows[-1][4],
        }

    return bars


def backtest_segment(segment, state):
    if len(segment) < 100:
        return

    times = [c[0] for c in segment]
    opens = [c[3] for c in segment]
    highs = [c[2] for c in segment]
    lows = [c[1] for c in segment]
    closes = [c[4] for c in segment]

    fast = ema(closes, 20)
    slow = ema(closes, 50)
    rsi_values = rsi(closes, 14)

    tr = []
    for i in range(len(segment)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))

    atr = ema(tr, 14)

    bars15 = build_15m(segment)
    times15 = sorted(bars15)
    closes15 = [bars15[t]["close"] for t in times15]
    ema20_15 = ema(closes15, 20)
    ema50_15 = ema(closes15, 50)

    trend = {}
    for j, t in enumerate(times15):
        if ema20_15[j] is not None and ema50_15[j] is not None:
            # Available only after the 15m candle has closed.
            trend[t + 900] = (
                ema20_15[j] > ema50_15[j],
                ema20_15[j] < ema50_15[j],
            )

    for i in range(60, len(segment) - 1):
        if any(x[i] is None for x in (fast, slow, rsi_values, atr)):
            continue
        if atr[i] <= 0:
            continue

        bucket = times[i] - times[i] % 900
        current_trend = trend.get(bucket)

        if current_trend is None:
            continue

        uptrend, downtrend = current_trend
        candle_range = highs[i] - lows[i]

        if candle_range <= 0:
            continue

        buy = (
            uptrend
            and fast[i] > slow[i]
            and closes[i] > fast[i]
            and closes[i - 1] <= fast[i - 1]
            and 52 <= rsi_values[i] <= 65
            and closes[i] >= lows[i] + 0.70 * candle_range
        )

        sell = (
            downtrend
            and fast[i] < slow[i]
            and closes[i] < fast[i]
            and closes[i - 1] >= fast[i - 1]
            and 35 <= rsi_values[i] <= 48
            and closes[i] <= highs[i] - 0.70 * candle_range
        )

        position = state["position"]
        next_open = opens[i + 1]
        next_high = highs[i + 1]
        next_low = lows[i + 1]
        next_time = times[i + 1]

        if position is None:
            if not (buy or sell):
                continue

            direction = 1 if buy else -1
            entry = next_open * (
                1 + direction * (SPREAD_RATE / 2 + SLIPPAGE_RATE)
            )

            stop_distance = max(atr[i] * 1.2, entry * 0.001)
            risk_cash = state["balance"] * RISK_FRACTION
            units_by_risk = risk_cash / stop_distance
            units_by_capital = (
                state["balance"] * MAX_NOTIONAL_FRACTION
                / (entry * (1 + FEE_RATE))
            )
            units = min(units_by_risk, units_by_capital)

            if units <= 0:
                continue

            entry_fee = units * entry * FEE_RATE

            state["position"] = {
                "direction": direction,
                "entry": entry,
                "time": next_time,
                "units": units,
                "entry_fee": entry_fee,
                "stop": entry - direction * stop_distance,
                "target": entry + direction * stop_distance * REWARD_RISK,
            }
            continue

        direction = position["direction"]

        stop_hit = (
            next_low <= position["stop"]
            if direction == 1
            else next_high >= position["stop"]
        )
        target_hit = (
            next_high >= position["target"]
            if direction == 1
            else next_low <= position["target"]
        )

        exit_price = None
        reason = None

        # If a candle touches both, count the stop first.
        if stop_hit:
            exit_price = position["stop"]
            reason = "STOP"
        elif target_hit:
            exit_price = position["target"]
            reason = "TARGET"
        elif (
            (direction == 1 and (sell or downtrend))
            or (direction == -1 and (buy or uptrend))
        ):
            exit_price = next_open
            reason = "SIGNAL"

        if exit_price is None:
            continue

        # Conservative fill adjustment for modeled spread/slippage.
        exit_price *= (
            1 - direction * (SPREAD_RATE / 2 + SLIPPAGE_RATE)
        )

        gross = (
            (exit_price - position["entry"])
            * position["units"] * direction
        )
        exit_fee = exit_price * position["units"] * FEE_RATE
        pnl = gross - exit_fee - position["entry_fee"]

        state["balance"] += pnl
        state["trades"].append({
            "pnl": pnl,
            "reason": reason,
            "entry_time": position["time"],
            "exit_time": next_time,
        })
        state["position"] = None


def main():
    segments = get_candles()
    total = sum(len(s) for s in segments)

    if total < 500:
        raise RuntimeError(
            f"Not enough data: {total} candles across "
            f"{len(segments)} continuous segments."
        )

    state = {
        "balance": INITIAL_BALANCE,
        "position": None,
        "trades": [],
    }

    for segment in segments:
        # Never carry an open position across a data gap.
        if state["position"] is not None:
            state["position"] = None
        backtest_segment(segment, state)

    trades = state["trades"]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]

    gross_wins = sum(t["pnl"] for t in wins)
    gross_losses = -sum(t["pnl"] for t in losses)
    pf = gross_wins / gross_losses if gross_losses else None

    print("BTC-USD 5m/15m historical signal test")
    print(f"Data candles: {total}")
    print(f"Continuous segments: {len(segments)}")
    print(f"Initial balance: ${INITIAL_BALANCE:.2f}")
    print(f"Closed balance: ${state['balance']:.2f}")
    print(
        f"Return: "
        f"{(state['balance'] / INITIAL_BALANCE - 1) * 100:.2f}%"
    )
    print(f"Closed trades: {len(trades)}")
    print(f"Wins: {len(wins)}")
    print(f"Losses: {len(losses)}")
    print(
        f"Win rate: {100 * len(wins) / len(trades):.2f}%"
        if trades else "Win rate: N/A"
    )
    print(f"Profit factor: {pf:.2f}" if pf is not None else
          "Profit factor: N/A")
    print("Fees, spread and slippage are estimates.")
    print("This test is experimental and does not predict future results.")


if __name__ == "__main__":
    main()
