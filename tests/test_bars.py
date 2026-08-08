import math

from summerand.market.bars import BarBuilder, PriceBook
from summerand.schemas import MINUTE_MS, Bar, Tick


def tick(ts, price, sym="BTC"):
    return Tick(symbol=sym, asset_class="crypto", price=price, size=1, ts_ms=ts, venue="t")


def test_bar_builder_closes_minutes():
    b = BarBuilder()
    assert b.add(tick(1_000, 10)) == []
    assert b.add(tick(30_000, 12)) == []
    done = b.add(tick(61_000, 11))
    assert len(done) == 1
    bar = done[0]
    assert (bar.ts_ms, bar.open, bar.high, bar.low, bar.close, bar.volume) == (0, 10, 12, 10, 12, 2)
    assert b.flush(119_999) == []
    assert [x.close for x in b.flush(120_000)] == [11]


def test_price_book_has_no_lookahead():
    pb = PriceBook()
    for i, close in enumerate([100, 101, 102]):
        pb.add(Bar(symbol="BTC", ts_ms=i * MINUTE_MS, open=close, high=close, low=close,
                   close=close, volume=1, venue="t"))
    # The bar for minute 0 is only known at 60s.
    assert pb.close_at("BTC", 59_999) is None
    assert pb.close_at("BTC", 60_000) == 100
    assert pb.close_at("BTC", 120_000, strict=True) == 100
    assert math.isclose(pb.log_return("BTC", 60_000, 180_000), math.log(102 / 100))
    assert pb.close_at("BTC", 180_000 + 31 * MINUTE_MS) is None  # stale
