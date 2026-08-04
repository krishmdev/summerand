"""Ticker extraction. extension/shared/tickers.js implements the same rules for page highlighting;
extension/tests/ticker_cases.json is run against both."""

from __future__ import annotations

import re
from dataclasses import dataclass

from summerand.config import Watchlist

_CASHTAG = re.compile(r"(?<![A-Za-z0-9$])\$([A-Z]{1,5}(?:\.[A-Z])?)(?![A-Za-z0-9])")
_EXCHANGE = re.compile(
    r"\((?:NASDAQ|Nasdaq|NYSE American|NYSE Arca|NYSE|AMEX|Cboe|CBOE)\s*:\s*([A-Z][A-Z.]{0,5})\)"
)
_BARE = re.compile(r"(?<![A-Za-z0-9$])[A-Z]{2,5}(?![A-Za-z0-9])")
_POLYGON = re.compile(r"(?<![A-Za-z0-9])Polygon(?![A-Za-z0-9.])")
_POL_CONTEXT = re.compile(
    r"\b(POL|MATIC|network|blockchain|layer[- ]?2|L2|zkEVM|AggLayer|token|chain|staking)\b",
    re.IGNORECASE,
)
_CRYPTO_CONTEXT = re.compile(r"\b(crypto|token|tokens|blockchain|coin|fees|staking|DeFi)\b", re.I)


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    symbol: str
    kind: str


def _mostly_upper(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 16:
        return False
    return sum(c.isupper() for c in letters) / len(letters) > 0.6


class TickerExtractor:
    def __init__(self, watchlist: Watchlist) -> None:
        self.watch = watchlist.by_symbol
        self.stoplist = watchlist.stoplist
        self.universe = watchlist.universe | set(self.watch)
        self.alias_to_symbol: dict[str, str] = {}
        self.legacy_to_symbol: dict[str, str] = {}
        for sym in watchlist.symbols:
            for alias in sym.aliases:
                self.alias_to_symbol[alias] = sym.symbol
            for old in sym.legacy:
                self.legacy_to_symbol[old] = sym.symbol
        aliases = sorted(self.alias_to_symbol, key=len, reverse=True)
        self._alias_re = (
            re.compile(
                r"(?<![A-Za-z0-9&])("
                + "|".join(re.escape(a) for a in aliases)
                + r")(?![A-Za-z0-9&])"
            )
            if aliases
            else None
        )

    def find(self, text: str) -> list[Match]:
        found: list[Match] = []
        taken: list[tuple[int, int]] = []

        def add(start: int, end: int, symbol: str, kind: str) -> None:
            if any(start < e and s < end for s, e in taken):
                return
            taken.append((start, end))
            found.append(Match(start, end, symbol, kind))

        for m in _CASHTAG.finditer(text):
            sym = self.legacy_to_symbol.get(m.group(1), m.group(1))
            if sym in self.universe:
                add(m.start(), m.end(), sym, "cashtag")
        for m in _EXCHANGE.finditer(text):
            add(m.start(1), m.end(1), m.group(1), "exchange")
        if self._alias_re is not None:
            for m in self._alias_re.finditer(text):
                add(m.start(), m.end(), self.alias_to_symbol[m.group(1)], "alias")

        if "POL" in self.watch and _POL_CONTEXT.search(text):
            for m in _POLYGON.finditer(text):
                add(m.start(), m.end(), "POL", "alias")

        if not _mostly_upper(text):
            symbols_so_far = {x.symbol for x in found}
            crypto_context = bool(_CRYPTO_CONTEXT.search(text)) or any(
                self.watch[s].asset_class == "crypto" for s in symbols_so_far if s in self.watch
            )
            for m in _BARE.finditer(text):
                word = m.group(0)
                if word in self.stoplist:
                    continue
                if word in self.legacy_to_symbol:
                    add(m.start(), m.end(), self.legacy_to_symbol[word], "legacy")
                    continue
                spec = self.watch.get(word)
                if spec is None or not spec.bare:
                    continue
                if word == "POL" and not crypto_context:
                    continue
                add(m.start(), m.end(), word, "bare")

        found.sort(key=lambda x: x.start)
        return found

    def extract(self, text: str) -> list[str]:
        return sorted({m.symbol for m in self.find(text)})
