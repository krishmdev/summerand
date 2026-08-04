import json

import pytest

from summerand.etl.tickers import TickerExtractor
from tests.conftest import ROOT

CASES = json.loads((ROOT / "extension/tests/ticker_cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["text"][:40] for c in CASES])
def test_shared_cases(case, watchlist):
    assert TickerExtractor(watchlist).extract(case["text"]) == sorted(case["expect"])


def test_spans_point_at_the_text(watchlist):
    text = "Shares of Nvidia and $AAPL rose"
    matches = TickerExtractor(watchlist).find(text)
    assert [(text[m.start : m.end], m.symbol) for m in matches] == [
        ("Nvidia", "NVDA"),
        ("$AAPL", "AAPL"),
    ]
