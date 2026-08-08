"""1-minute bars from ticks, and a price book the impact score queries.

A bar for minute [m, m+60s) only becomes visible at m+60s, so a price lookup at time t never sees
trades after t.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict

import numpy as np

from summerand.schemas import MINUTE_MS, Bar, Tick


class BarBuilder:
    def __init__(self) -> None:
        self._open: dict[str, Bar] = {}

    def add(self, tick: Tick) -> list[Bar]:
        minute = tick.ts_ms - tick.ts_ms % MINUTE_MS
        done: list[Bar] = []
        bar = self._open.get(tick.symbol)
        if bar is not None and bar.ts_ms != minute:
            if minute < bar.ts_ms:
                return done  # a straggler for an already-closed minute
            done.append(bar)
            bar = None
        if bar is None:
            self._open[tick.symbol] = Bar(
                symbol=tick.symbol,
                ts_ms=minute,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.size,
                venue=tick.venue,
            )
        else:
            bar.high = max(bar.high, tick.price)
            bar.low = min(bar.low, tick.price)
            bar.close = tick.price
            bar.volume += tick.size
        return done

    def flush(self, now_ms: int) -> list[Bar]:
        """Close every open bar whose minute has ended by now_ms."""
        done = [b for b in self._open.values() if b.end_ms <= now_ms]
        for b in done:
            del self._open[b.symbol]
        return sorted(done, key=lambda b: (b.ts_ms, b.symbol))


class PriceBook:
    """Closes keyed by bar end time. Stale prices (older than max_age) count as missing."""

    def __init__(self, max_age_ms: int = 30 * MINUTE_MS) -> None:
        self._t: dict[str, list[int]] = defaultdict(list)
        self._c: dict[str, list[float]] = defaultdict(list)
        self.max_age_ms = max_age_ms

    def add(self, bar: Bar) -> None:
        ts, closes = self._t[bar.symbol], self._c[bar.symbol]
        end = bar.end_ms
        if ts and end <= ts[-1]:
            i = bisect.bisect_left(ts, end)
            if i < len(ts) and ts[i] == end:
                closes[i] = bar.close
                return
            ts.insert(i, end)
            closes.insert(i, bar.close)
            return
        ts.append(end)
        closes.append(bar.close)

    def symbols(self) -> list[str]:
        return sorted(s for s, v in self._t.items() if v)

    def has(self, symbol: str) -> bool:
        return bool(self._t.get(symbol))

    def close_at(self, symbol: str, t_ms: int, strict: bool = False) -> float | None:
        """Last close known at t_ms (or strictly before it)."""
        ts = self._t.get(symbol)
        if not ts:
            return None
        i = (bisect.bisect_left(ts, t_ms) if strict else bisect.bisect_right(ts, t_ms)) - 1
        if i < 0 or t_ms - ts[i] > self.max_age_ms:
            return None
        return self._c[symbol][i]

    def log_return(
        self, symbol: str, t0_ms: int, t1_ms: int, strict_start: bool = False
    ) -> float | None:
        p0 = self.close_at(symbol, t0_ms, strict=strict_start)
        p1 = self.close_at(symbol, t1_ms)
        if p0 is None or p1 is None or p0 <= 0 or p1 <= 0:
            return None
        return math.log(p1 / p0)

    def minute_returns(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        """(bar end, 1-minute log return) pairs for consecutive bars inside [start, end]."""
        ts = self._t.get(symbol)
        if not ts:
            return []
        closes = self._c[symbol]
        lo = bisect.bisect_left(ts, start_ms)
        hi = bisect.bisect_right(ts, end_ms)
        out = []
        for i in range(max(lo, 1), hi):
            gap = ts[i] - ts[i - 1]
            if gap <= 0 or gap > 5 * MINUTE_MS or closes[i - 1] <= 0:
                continue
            # Scale multi-minute gaps to a per-minute return so sparse bars don't inflate vol.
            out.append((ts[i], math.log(closes[i] / closes[i - 1]) / math.sqrt(gap / MINUTE_MS)))
        return out

    def realized_vol(self, symbol: str, start_ms: int, end_ms: int) -> float | None:
        rets = [r for _, r in self.minute_returns(symbol, start_ms, end_ms)]
        if len(rets) < 10:
            return None
        return float(np.std(rets, ddof=1))

    def prune(self, before_ms: int) -> None:
        for sym in list(self._t):
            i = bisect.bisect_left(self._t[sym], before_ms)
            if i:
                del self._t[sym][:i]
                del self._c[sym][:i]
