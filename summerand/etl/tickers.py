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
_CRYPTO_CONTEXT = re.compile(
    r"\b(crypto|cryptocurrency|token|tokens|blockchain|coin|coins|fees|staking|DeFi|stablecoin|"
    r"exchange|wallet|on-chain|mainnet)\b",
    re.I,
)
_MARKET_CONTEXT = re.compile(
    r"(\b(shares|stock|stocks|earnings|revenue|investors|market|markets|Nasdaq|NYSE|analyst|"
    r"analysts|quarter|quarterly|guidance|valuation|trading|traders|rally|sell-off|index|"
    r"profit|sales|CEO|deliveries|iPhone)\b|S&P|%|\$\d)",
    re.I,
)


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

    @staticmethod
    def _followed_by(text: str, end: int, words: tuple[str, ...]) -> bool:
        after = text[end:].lstrip().lower()
        return any(re.match(re.escape(w.lower()) + r"\b", after) for w in words)

    def _context_ok(self, symbol: str, text: str, found: list[Match], alias: str = "") -> bool:
        spec = self.watch.get(symbol)
        if spec is None or spec.context is None or " " in alias:
            return True  # multi-word aliases ("Meta Platforms") are unambiguous on their own
        if spec.context == "market":
            return bool(_MARKET_CONTEXT.search(text))
        return bool(_CRYPTO_CONTEXT.search(text)) or any(
            m.symbol != symbol and self.watch.get(m.symbol, spec).asset_class == "crypto"
            for m in found
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
            if m.group(1) in self.universe:
                add(m.start(1), m.end(1), m.group(1), "exchange")

        # Ungated matches first, so they can supply crypto context for the gated ones.
        gated: list[tuple[int, int, str, str, str]] = []
        if self._alias_re is not None:
            for m in self._alias_re.finditer(text):
                sym = self.alias_to_symbol[m.group(1)]
                spec = self.watch.get(sym)
                if spec is not None and self._followed_by(text, m.end(), spec.not_before):
                    continue
                if spec is not None and spec.context and " " not in m.group(1):
                    gated.append((m.start(), m.end(), sym, "alias", m.group(1)))
                else:
                    add(m.start(), m.end(), sym, "alias")

        if "POL" in self.watch and _POL_CONTEXT.search(text):
            for m in _POLYGON.finditer(text):
                add(m.start(), m.end(), "POL", "alias")

        if not _mostly_upper(text):
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
                if self._followed_by(text, m.end(), spec.not_before):
                    continue
                if word == "POL" or spec.context:
                    gated.append((m.start(), m.end(), word, "bare", word))
                else:
                    add(m.start(), m.end(), word, "bare")

        for start, end, sym, kind, alias in gated:
            if sym == "POL":
                ok = bool(_CRYPTO_CONTEXT.search(text)) or any(
                    self.watch.get(m.symbol) is not None
                    and self.watch[m.symbol].asset_class == "crypto"
                    for m in found
                )
            else:
                ok = self._context_ok(sym, text, found, alias)
            if ok:
                add(start, end, sym, kind)

        found.sort(key=lambda x: x.start)
        return found

    def extract(self, text: str) -> list[str]:
        return sorted({m.symbol for m in self.find(text)})
