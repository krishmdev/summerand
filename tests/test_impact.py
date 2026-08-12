import math

import numpy as np
import pytest

from summerand.market.bars import PriceBook
from summerand.rank import impact
from summerand.schemas import MINUTE_MS, Bar


def test_velocity_check_values():
    assert impact.velocity(5, 0) == pytest.approx(0.9933, abs=1e-4)
    assert impact.velocity(0, 20) == pytest.approx(0.2529, abs=1e-4)


def test_credibility_check_values():
    assert impact.credibility([0.9]) == pytest.approx(0.3541, abs=1e-4)
    assert impact.credibility([0.7, 0.8, 0.9]) == pytest.approx(0.6215, abs=1e-4)


def test_recency_half_life():
    assert impact.recency(30 * MINUTE_MS, 0) == pytest.approx(0.7071, abs=1e-4)
    assert impact.recency(0, 0) == 1.0


def test_weights_renormalize_without_market():
    comps = {"velocity": 1.0, "credibility": 0.5, "recency": 1.0, "market": None, "novelty": 0.0}
    assert impact.combine(comps) == pytest.approx((0.3 + 0.1 + 0.2) / 0.8)
    assert impact.combine({**comps, "market": 1.0}) == pytest.approx(0.3 + 0.1 + 0.2 + 0.2)


def test_novelty():
    a = np.array([1.0, 0.0])
    assert impact.novelty(a, []) == 1.0
    assert impact.novelty(a, [np.array([1.0, 0.0])]) == 0.0
    assert impact.novelty(a, [np.array([0.0, 1.0])]) == 1.0


def test_ema():
    assert impact.ema(None, 0.8) == 0.8
    assert impact.ema(0.4, 0.8) == pytest.approx(0.6)


def test_eligibility():
    assert impact.eligible(2, 0.5, None)
    assert not impact.eligible(1, 0.95, None)
    assert impact.eligible(1, 0.95, 0.85)
    assert not impact.eligible(1, 0.8, 0.85)


def book(closes, start=0):
    pb = PriceBook(max_age_ms=10**12)
    for i, c in enumerate(closes):
        pb.add(
            Bar(
                symbol="SOL",
                ts_ms=start + i * MINUTE_MS,
                open=c,
                high=c,
                low=c,
                close=c,
                volume=1,
                venue="t",
            )
        )
    return pb


def test_market_move_sigma_normalized():
    rng = np.random.default_rng(0)
    quiet = list(100 * np.exp(np.cumsum(rng.normal(0, 0.001, 121))))
    shocked = quiet + [quiet[-1] * math.exp(-0.02)] * 10
    pb = book(shocked)
    first = 121 * MINUTE_MS  # first report right as the drop starts
    now = first + 10 * MINUTE_MS
    m, moves = impact.market_move(pb, ["SOL"], [first, first + MINUTE_MS], now)
    sigma = pb.realized_vol("SOL", first - 120 * MINUTE_MS, first)
    r = math.log(pb.close_at("SOL", now) / pb.close_at("SOL", first, strict=True))
    expected_m = abs(r) / (sigma * math.sqrt(10))
    assert moves[0].sigmas == pytest.approx(-expected_m, rel=1e-3)
    assert moves[0].pct == pytest.approx(math.expm1(r) * 100, rel=1e-6)
    assert 0.7 * (1 - math.exp(-expected_m / 2)) <= m <= 1.0
    assert impact.market_move(pb, ["NVDA"], [first], now) == (None, [])


def test_priced_tickers_need_thirty_percent_coverage():
    pb = book([1, 2, 3])
    assert impact.priced_tickers([["SOL"], ["SOL"], [], [], [], [], []], pb) == []
    assert impact.priced_tickers([["SOL"], ["SOL"], ["SOL"], [], [], [], []], pb) == ["SOL"]
