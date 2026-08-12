"""Impact score for a cluster at event time t.

    velocity     V = sigmoid(z), z = (n_recent - e) / sqrt(e + 1), e = n_prior * D / (W - D)
    credibility  S = mean(prior of each distinct source) * (1 - exp(-n_distinct / 2))
    recency      R = exp(-ln2 * (t - t_latest) / H)
    market       M = 0.7 * (1 - exp(-max_k m_k / 2)) + 0.3 * max(0, max_k rho_k)
    novelty      N = 1 - max cosine to other clusters surfaced in the last 24h

with D = 30 min, W = 6 h, H = 60 min. m_k is the absolute log return of ticker k since the last
close before the first report, in units of sigma_1m * sqrt(minutes since), and rho_k is the best
Pearson correlation between the cluster's per-minute article counts and |1-min return| when price
leads coverage by 0-5 minutes. Impact is the weighted mean over the components that are available,
so a cluster without priced tickers is scored on the other four with renormalized weights.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

import numpy as np

from summerand.clock import HOUR_MS, MINUTE_MS
from summerand.market.bars import PriceBook

DELTA_MS = 30 * MINUTE_MS
WINDOW_MS = 6 * HOUR_MS
HALF_LIFE_MS = 60 * MINUTE_MS
WEIGHTS = {"velocity": 0.30, "credibility": 0.20, "recency": 0.20, "market": 0.20, "novelty": 0.10}
EMA_ALPHA = 0.5


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def velocity(
    n_recent: int, n_prior: int, delta_ms: int = DELTA_MS, window_ms: int = WINDOW_MS
) -> float:
    expected = n_prior * delta_ms / (window_ms - delta_ms)
    return sigmoid((n_recent - expected) / math.sqrt(expected + 1))


def credibility(priors: list[float]) -> float:
    if not priors:
        return 0.0
    return float(np.mean(priors)) * (1 - math.exp(-len(priors) / 2))


def recency(now_ms: int, latest_ms: int, half_life_ms: int = HALF_LIFE_MS) -> float:
    return math.exp(-math.log(2) * max(0, now_ms - latest_ms) / half_life_ms)


def novelty(centroid: np.ndarray, others: list[np.ndarray]) -> float:
    if not others:
        return 1.0
    return float(min(1.0, max(0.0, 1.0 - max(float(o @ centroid) for o in others))))


def combine(components: dict[str, float | None]) -> float:
    avail = {k: v for k, v in components.items() if v is not None}
    total = sum(WEIGHTS[k] for k in avail)
    return sum(WEIGHTS[k] * v for k, v in avail.items()) / total if total else 0.0


def ema(prev: float | None, x: float, alpha: float = EMA_ALPHA) -> float:
    return x if prev is None else alpha * x + (1 - alpha) * prev


@dataclass
class TickerMove:
    symbol: str
    since_ms: int
    pct: float
    sigmas: float


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3 or a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def priced_tickers(ticker_lists: list[list[str]], prices: PriceBook) -> list[str]:
    counts = Counter(t for ts in ticker_lists for t in set(ts))
    need = max(1, math.ceil(0.3 * len(ticker_lists)))
    return sorted(t for t, c in counts.items() if c >= need and prices.has(t))


def market_move(
    prices: PriceBook,
    tickers: list[str],
    published: list[int],
    now_ms: int,
    lookback_min: int = 120,
    max_lag: int = 5,
) -> tuple[float | None, list[TickerMove]]:
    if not tickers or not published:
        return None, []
    first = min(published)
    minutes = max(5.0, (now_ms - first) / MINUTE_MS)
    start = now_ms - now_ms % MINUTE_MS - lookback_min * MINUTE_MS
    counts = np.zeros(lookback_min)
    for ts in published:
        j = (ts - start) // MINUTE_MS
        if 0 <= j < lookback_min:
            counts[j] += 1
    best_m, best_rho, moves = None, 0.0, []
    for sym in tickers:
        p0 = prices.close_at(sym, first, strict=True)
        p1 = prices.close_at(sym, now_ms)
        sigma = prices.realized_vol(sym, first - lookback_min * MINUTE_MS, first)
        if p0 is None or p1 is None or sigma is None:
            continue
        r = math.log(p1 / p0)
        m = abs(r) / (sigma * math.sqrt(minutes) + 1e-6)
        best_m = m if best_m is None else max(best_m, m)
        moves.append(
            TickerMove(sym, first, (math.exp(r) - 1) * 100, r / (sigma * math.sqrt(minutes) + 1e-6))
        )
        absret = np.zeros(lookback_min)
        for end, ret in prices.minute_returns(sym, start, now_ms):
            j = (end - MINUTE_MS - start) // MINUTE_MS
            if 0 <= j < lookback_min:
                absret[j] = abs(ret)
        for lag in range(max_lag + 1):
            a = absret[: lookback_min - lag]
            b = counts[lag:]
            best_rho = max(best_rho, _pearson(a, b))
    if best_m is None:
        return None, []
    return 0.7 * (1 - math.exp(-best_m / 2)) + 0.3 * max(0.0, best_rho), moves


def eligible(n_members: int, top_source_prior: float, market: float | None) -> bool:
    if n_members >= 2:
        return True
    return top_source_prior >= 0.9 and market is not None and market >= 0.8
