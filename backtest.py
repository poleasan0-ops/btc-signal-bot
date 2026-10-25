
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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


def fetch_candles():
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - DAYS * 86400
    candles = {}

    # Coinbase allows at most 300 candles per request.
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
            headers={"User-Agent": "HistoricalSignalTest/6.0"},
        )

        last_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                    request, timeout=30
                ) as response:
                    data = json.loads(response.read().decode())
                if not isinstance(data, list):
                    raise RuntimeError(f"API error: {data}")
                for row in data:
                    ts = int(row[0])
                    if start <= ts and ts + GRANULARITY <= end:
                        candles[ts] = {
                            "ts": ts,
                            "low": float(row[1]),
                            "high": float(row[2]),
                            "open": float(row[3]),
                            "close": float(row[4]),
                            "volume": float(row[5]),
                        }
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                time.sleep(1 + attempt)

        if last_error:
            raise RuntimeError(
                f"Failed to download candles: {last_error}"
            )
        cursor = chunk_end
        time.sleep(0.25)

    rows = [candles[t] for t in sorted(candles)]
    if len(rows) < 500:
        raise RuntimeError(f"Not enough candles: {len(rows)}")

    gaps = sum(
        rows[i]["ts"] != rows[i - 1]["ts"] + GRANULARITY
        for i in range(1, len(rows))
    )
    print(f"Downloaded candles: {len(rows)}")
    print(f"Missing-candle boundaries: {gaps}")
    return rows


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
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calc(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100 - 100 / (1 + gain / loss)

    out[period] = calc(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        k = i - 1
        avg_gain = (
            avg_gain * (period - 1) + gains[k]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[k]
        ) / period
        out[i] = calc(avg_gain, avg_loss)
    return out


def build_signals(rows):
    close = [r["close"] for r in rows]
    high = [r["high"] for r in rows]
    low = [r["low"] for r in rows]

    e20 = ema(close, 20)
    e50 = ema(close, 50)
    rv = rsi(close)

    # True range and ATR.
    tr = []
    for i, row in enumerate(rows):
        if i == 0:
            tr.append(high[i] - low[i])
        else:
            tr.append(max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            ))
    atr = ema(tr, 14)

    signals = {}
    for i in range(60, len(rows)):
        if any(x[i] is None for x in (e20, e50, rv, atr)):
            continue
        if e20[i] is None or e50[i] is None or atr[i] <= 0:
            continue

        # Strategy A: trend plus RSI.
        buy = (
            e20[i] > e50[i]
            and close[i] > e20[i]
            and close[i - 1] <= e20[i - 1]
            and 52 <= rv[i] <= 65
        )
        sell = (
            e20[i] < e50[i]
            and close[i] < e20[i]
            and close[i - 1] >= e20[i - 1]
            and 35 <= rv[i] <= 48
        )

        # Strategy B: EMA trend without RSI restriction.
        trend_buy = (
            e20[i] > e50[i]
            and close[i] > e20[i]
            and close[i - 1] <= e20[i - 1]
        )
        trend_sell = (
            e20[i] < e50[i]
            and close[i] < e20[i]
            and close[i - 1] >= e20[i - 1]
        )

        # Strategy C: breakout of previous 20 candles.
        start = max(0, i - 20)
        previous_high = max(high[start:i])
        previous_low = min(low[start:i])
        breakout_buy = (
            close[i] > previous_high and e20[i] > e50[i]
        )
        breakout_sell = (
            close[i] < previous_low and e20[i] < e50[i]
        )

        signals["trend_rsi", i] = (
            1 if buy else -1 if sell else 0, atr[i]
        )
        signals["trend", i] = (
            1 if trend_buy else -1 if trend_sell else 0, atr[i]
        )
        signals["breakout", i] = (
            1 if breakout_buy else -1 if breakout_sell else 0,
            atr[i],
        )

    return signals


def backtest(rows, signals, strategy, begin, finish):
    balance = INITIAL_BALANCE
    peak = INITIAL_BALANCE
    max_drawdown = 0.0
    position = None
    trades = []
    equity_curve = []

    for i in range(max(begin, 60), min(finish, len(rows) - 1)):
        row = rows[i]
        next_row = rows[i + 1]

        # Reset at data gaps; do not pretend a trade can cross a gap.
        if row["ts"] + GRANULARITY != next_row["ts"]:
            if position:
                d = position["direction"]
                exit_price = row["close"] * (
                    1 - d * (SPREAD / 2 + SLIPPAGE)
                )
                pnl = (
                    (exit_price - position["entry"])
                    * position["units"] * d
                    - exit_price * position["units"] * FEE
                    - position["entry_fee"]
                )
                balance += pnl
                trades.append(pnl)
                position = None
            equity_curve.append(balance)
            peak = max(peak, balance)
            continue

        signal, atr = signals.get(
            (strategy, i), (0, None)
        )

        if position:
            d = position["direction"]
            stop_hit = (
                next_row["low"] <= position["stop"]
                if d == 1 else next_row["high"] >= position["stop"]
            )
            target_hit = (
                next_row["high"] >= position["target"]
                if d == 1 else next_row["low"] <= position["target"]
            )

            exit_price = None
            if stop_hit:
                exit_price = position["stop"]
            elif target_hit:
                exit_price = position["target"]
            elif signal == -d:
                exit_price = next_row["open"]

            if exit_price is not None:
                exit_price *= 1 - d * (SPREAD / 2 + SLIPPAGE)
                pnl = (
                    (exit_price - position["entry"])
                    * position["units"] * d
                    - exit_price * position["units"] * FEE
                    - position["entry_fee"]
                )
                balance += pnl
                trades.append(pnl)
                position = None

        if position is None and signal != 0 and atr:
            d = signal
            entry = next_row["open"] * (
                1 + d * (SPREAD / 2 + SLIPPAGE)
            )
            distance = max(atr * 1.2, entry * 0.001)
            risk_cash = balance * RISK
            by_risk = risk_cash / distance
            by_capital = balance * MAX_EXPOSURE / (
                entry * (1 + FEE)
            )
            units = min(by_risk, by_capital)

            if units > 0:
                position = {
                    "direction": d,
                    "entry": entry,
                    "units": units,
                    "entry_fee": entry * units * FEE,
                    "stop": entry - d * distance,
                    "target": entry + d * distance * RR,
                }

        equity = balance
        if position:
            equity += (
                (row["close"] - position["entry"])
                * position["units"] * position["direction"]
                - position["entry_fee"]
            )
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(
                max_drawdown, (peak - equity) / peak
            )
        equity_curve.append(equity)

    # Mark any final open trade to market, including exit costs.
    if position:
        d = position["direction"]
        exit_price = rows[min(finish, len(rows) - 1)]["close"]
        exit_price *= 1 - d * (SPREAD / 2 + SLIPPAGE)
        pnl = (
            (exit_price - position["entry"])
            * position["units"] * d
            - exit_price * position["units"] * FEE
            - position["entry_fee"]
        )
        balance += pnl
        trades.append(pnl)
        position = None

    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    pf = gross_win / gross_loss if gross_loss else None

    return {
        "balance": balance,
        "return_pct": (balance / INITIAL_BALANCE - 1) * 100,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "profit_factor": pf,
        "max_drawdown": max_drawdown * 100,
        "avg_trade": sum(trades) / len(trades) if trades else 0,
        "trades_pnl": trades,
    }


def report(title, result):
    print(f"\n{title}")
    print("-" * len(title))
    print(f"Ending balance: ${result['balance']:.2f}")
    print(f"Net return: {result['return_pct']:.2f}%")
    print(f"Closed trades: {result['trades']}")
    print(f"Wins / losses: {result['wins']} / {result['losses']}")
    print(f"Win rate: {result['win_rate']:.2f}%")
    print(
        f"Profit factor: {result['profit_factor']:.2f}"
        if result["profit_factor"] is not None
        else "Profit factor: N/A"
    )
    print(f"Max drawdown estimate: {result['max_drawdown']:.2f}%")
    print(f"Average net P/L per trade: ${result['avg_trade']:.2f}")

    if result["trades"] < 30:
        print("Assessment: too few trades for a reliable conclusion.")
    elif (
        result["return_pct"] > 0
        and result["profit_factor"] is not None
        and result["profit_factor"] > 1.1
    ):
        print("Assessment: promising, but further testing is required.")
    else:
        print("Assessment: not ready for live signal use.")


def main():
    rows = fetch_candles()
    signals = build_signals(rows)

    # Chronological split: first 70% development, final 30% test.
    split = int(len(rows) * 0.70)
    strategies = ("trend_rsi", "trend", "breakout")

    print("\nBTC-USD historical backtest")
    print("No live orders are sent. Historical results do not guarantee profit.")

    for strategy in strategies:
        # Independent test-period balance; no tuning on test data.
        result = backtest(
            rows, signals, strategy, split, len(rows) - 1
        )
        report(f"{strategy} - out-of-sample 30%", result)


if __name__ == "__main__":
    main()
