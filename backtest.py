
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from collections import defaultdict

URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GRANULARITY = 300
DAYS = 30
INITIAL_BALANCE = 1000.0

FEE = 0.0005
SLIPPAGE = 0.0002
SPREAD = 0.0002
RISK = 0.005
RR = 1.5
MAX_EXPOSURE = 1.0


def fetch_segments():
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - DAYS * 86400
    all_data = {}
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
            headers={"User-Agent": "HistoricalSignalTest/5.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())

        if not isinstance(data, list):
            raise RuntimeError(f"API error: {data}")

        for c in data:
            ts = int(c[0])
            if start <= ts and ts + GRANULARITY <= end:
                all_data[ts] = [
                    ts, float(c[1]), float(c[2]),
                    float(c[3]), float(c[4]), float(c[5])
                ]
        cursor = chunk_end

    rows = [all_data[t] for t in sorted(all_data)]
    segments, current = [], []

    for c in rows:
        if current and c[0] != current[-1][0] + GRANULARITY:
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
    changes = [values[i] - values[i - 1]
               for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period

    def calc(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[period] = calc(ag, al)
    for i in range(period + 1, len(values)):
        k = i - 1
        ag = (ag * (period - 1) + gains[k]) / period
        al = (al * (period - 1) + losses[k]) / period
        out[i] = calc(ag, al)
    return out


def make_15m(segment):
    groups = defaultdict(list)
    for c in segment:
        bucket = c[0] - c[0] % 900
        groups[bucket].append(c)

    bars = {}
    for bucket, group in groups.items():
        group.sort(key=lambda x: x[0])
        if [x[0] for x in group] == [
            bucket, bucket + 300, bucket + 600
        ]:
            bars[bucket] = group[-1][4]
    return bars


def run_segment(segment, state):
    if len(segment) < 100:
        return

    ts = [c[0] for c in segment]
    op = [c[3] for c in segment]
    hi = [c[2] for c in segment]
    lo = [c[1] for c in segment]
    cl = [c[4] for c in segment]

    e20, e50 = ema(cl, 20), ema(cl, 50)
    rv = rsi(cl)
    tr = []
    for i in range(len(segment)):
        if i == 0:
            tr.append(hi[i] - lo[i])
        else:
            tr.append(max(
                hi[i] - lo[i],
                abs(hi[i] - cl[i - 1]),
                abs(lo[i] - cl[i - 1]),
            ))
    av = ema(tr, 14)

    bars15 = make_15m(segment)
    t15 = sorted(bars15)
    c15 = [bars15[t] for t in t15]
    f15, s15 = ema(c15, 20), ema(c15, 50)
    trends = {}

    for j, t in enumerate(t15):
        if f15[j] is not None and s15[j] is not None:
            trends[t + 900] = (f15[j] > s15[j], f15[j] < s15[j])

    for i in range(60, len(segment) - 1):
        if any(x[i] is None for x in (e20, e50, rv, av)):
            continue
        if av[i] <= 0:
            continue

        bucket = ts[i] - ts[i] % 900
        trend = trends.get(bucket)
        if trend is None:
            continue
        up, down = trend

        width = hi[i] - lo[i]
        if width <= 0:
            continue

        buy = (
            up and e20[i] > e50[i] and cl[i] > e20[i]
            and cl[i - 1] <= e20[i - 1]
            and 52 <= rv[i] <= 65
            and cl[i] >= lo[i] + 0.70 * width
        )
        sell = (
            down and e20[i] < e50[i] and cl[i] < e20[i]
            and cl[i - 1] >= e20[i - 1]
            and 35 <= rv[i] <= 48
            and cl[i] <= hi[i] - 0.70 * width
        )

        pos = state["position"]
        next_open = op[i + 1]
        next_hi, next_lo = hi[i + 1], lo[i + 1]
        next_ts = ts[i + 1]

        # Handle an existing position first; never reopen on same bar.
        if pos is not None:
            d = pos["direction"]
            stop_hit = next_lo <= pos["stop"] if d == 1 else next_hi >= pos["stop"]
            target_hit = next_hi >= pos["target"] if d == 1 else next_lo <= pos["target"]

            exit_price, reason = None, None
            if stop_hit:
                exit_price, reason = pos["stop"], "STOP"
            elif target_hit:
                exit_price, reason = pos["target"], "TARGET"
            elif (d == 1 and (sell or down)) or (d == -1 and (buy or up)):
                exit_price, reason = next_open, "SIGNAL"

            if exit_price is not None:
                exit_price *= 1 - d * (SPREAD / 2 + SLIPPAGE)
                gross = (exit_price - pos["entry"]) * pos["units"] * d
                exit_fee = exit_price * pos["units"] * FEE
                pnl = gross - exit_fee - pos["entry_fee"]
                state["balance"] += pnl
                state["trades"].append({
                    "pnl": pnl, "reason": reason
                })
                state["position"] = None
                # No new entry on this same bar.
                continue

        if state["position"] is not None or not (buy or sell):
            continue

        d = 1 if buy else -1
        entry = next_open * (1 + d * (SPREAD / 2 + SLIPPAGE))
        distance = max(av[i] * 1.2, entry * 0.001)
        risk_cash = state["balance"] * RISK
        by_risk = risk_cash / distance
        by_capital = (
            state["balance"] * MAX_EXPOSURE / (entry * (1 + FEE))
        )
        units = min(by_risk, by_capital)
        if units <= 0:
            continue

        state["position"] = {
            "direction": d,
            "entry": entry,
            "units": units,
            "entry_fee": entry * units * FEE,
            "stop": entry - d * distance,
            "target": entry + d * distance * RR,
        }

        # Track equity each bar, including open-position unrealized P/L.
        pos = state["position"]
        mark = cl[i + 1]
        equity = state["balance"] + (
            (mark - pos["entry"]) * pos["units"] * pos["direction"]
            - pos["entry_fee"]
        )
        state["peak"] = max(state["peak"], equity)
        if state["peak"] > 0:
            dd = (state["peak"] - equity) / state["peak"]
            state["max_dd"] = max(state["max_dd"], dd)


def main():
    segments = fetch_segments()
    total = sum(len(s) for s in segments)
    if total < 500:
        raise RuntimeError(f"Not enough candles: {total}")

    state = {
        "balance": INITIAL_BALANCE,
        "position": None,
        "trades": [],
        "peak": INITIAL_BALANCE,
        "max_dd": 0.0,
    }

    for segment in segments:
        # Do not carry a position over missing market data.
        if state["position"] is not None:
            state["position"] = None
        run_segment(segment, state)

    trades = state["trades"]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    pf = gw / gl if gl else None

    print("BTC-USD historical signal backtest")
    print(f"Candles: {total}")
    print(f"Continuous segments: {len(segments)}")
    print(f"Initial balance: ${INITIAL_BALANCE:.2f}")
    print(f"Closed balance: ${state['balance']:.2f}")
    print(f"Return: {(state['balance'] / INITIAL_BALANCE - 1) * 100:.2f}%")
    print(f"Closed trades: {len(trades)}")
    print(f"Wins: {len(wins)}")
    print(f"Losses: {len(losses)}")
    print(f"Win rate: {100 * len(wins) / len(trades):.2f}%" if trades else "Win rate: N/A")
    print(f"Profit factor: {pf:.2f}" if pf is not None else "Profit factor: N/A")
    print(f"Max drawdown estimate: {state['max_dd'] * 100:.2f}%")
    print("Research only. No orders are placed. No future profit is guaranteed.")


if __name__ == "__main__":
    main()
