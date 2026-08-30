from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kafka_broker: str = "localhost:19092"
    summerand_topic_prefix: str = ""
    summerand_extension_ids: str = ""
    database_url: str = "sqlite:///var/summerand.sqlite"

    openai_api_key: SecretStr | None = None
    polygon_api_key: SecretStr | None = None
    cryptopanic_token: SecretStr | None = None
    cryptopanic_url: str | None = None
    polygon_ws_url: str = "wss://socket.polygon.io/stocks"
    coinbase_ws_url: str = "wss://ws-feed.exchange.coinbase.com"

    summerand_embedder: Literal["auto", "openai", "local", "hashing"] = "auto"
    summerand_llm: Literal["auto", "openai", "off"] = "auto"
    summerand_embed_model: str = "text-embedding-3-small"
    summerand_brief_model: str = "gpt-4.1-mini"
    summerand_config_dir: Path = REPO_ROOT / "config"
    summerand_models_dir: Path = REPO_ROOT / ".models"
    # "require-blocked" makes every process type check at startup that it cannot reach the internet.
    summerand_egress_check: Literal["off", "require-blocked"] = "off"

    @cached_property
    def sources(self) -> SourcesConfig:
        return load_sources(self.summerand_config_dir / "sources.yaml")

    @cached_property
    def watchlist(self) -> Watchlist:
        return load_watchlist(self.summerand_config_dir)

    def secret(self, name: str) -> str | None:
        value = getattr(self, name)
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        return raw or None


@dataclass(frozen=True)
class FeedConfig:
    name: str
    url: str
    credibility: float
    enabled: bool = True


@dataclass(frozen=True)
class SourcesConfig:
    feeds: list[FeedConfig]
    poll_seconds: int = 60
    cryptopanic_url: str | None = None
    cryptopanic_poll_seconds: int = 120
    cryptopanic_credibility: float = 0.5
    default_credibility: float = 0.5

    def credibility(self, source: str) -> float:
        if source == "cryptopanic" or source.startswith("cryptopanic:"):
            return self.cryptopanic_credibility
        for feed in self.feeds:
            if feed.name == source:
                return feed.credibility
        return self.default_credibility

    @property
    def enabled_feeds(self) -> list[FeedConfig]:
        return [f for f in self.feeds if f.enabled]


def load_sources(path: Path) -> SourcesConfig:
    data = yaml.safe_load(path.read_text())
    feeds = [
        FeedConfig(
            name=f["name"],
            url=f["url"],
            credibility=float(f.get("credibility", data.get("default_credibility", 0.5))),
            enabled=bool(f.get("enabled", True)),
        )
        for f in data.get("feeds", [])
    ]
    cp = data.get("cryptopanic") or {}
    return SourcesConfig(
        feeds=feeds,
        poll_seconds=int(data.get("poll_seconds", 60)),
        cryptopanic_url=cp.get("url"),
        cryptopanic_poll_seconds=int(cp.get("poll_seconds", 120)),
        cryptopanic_credibility=float(cp.get("credibility", 0.5)),
        default_credibility=float(data.get("default_credibility", 0.5)),
    )


@dataclass(frozen=True)
class Symbol:
    symbol: str
    asset_class: str
    aliases: tuple[str, ...] = ()
    bare: bool = True
    product: str | None = None
    legacy: tuple[str, ...] = ()


@dataclass
class Watchlist:
    symbols: list[Symbol]
    stoplist: frozenset[str]
    universe: frozenset[str] = field(default_factory=frozenset)

    @property
    def by_symbol(self) -> dict[str, Symbol]:
        return {s.symbol: s for s in self.symbols}

    def asset_class(self, symbol: str) -> str:
        s = self.by_symbol.get(symbol)
        return s.asset_class if s else "equity"

    def to_json(self) -> dict:
        return {
            "symbols": [
                {
                    "symbol": s.symbol,
                    "asset_class": s.asset_class,
                    "aliases": list(s.aliases),
                    "bare": s.bare,
                    "legacy": list(s.legacy),
                }
                for s in self.symbols
            ],
            "stoplist": sorted(self.stoplist),
        }


def load_watchlist(config_dir: Path) -> Watchlist:
    data = yaml.safe_load((config_dir / "watchlist.yaml").read_text())
    symbols: list[Symbol] = []
    for asset_class in ("crypto", "equities"):
        for row in data.get(asset_class, []):
            symbols.append(
                Symbol(
                    symbol=row["symbol"],
                    asset_class="crypto" if asset_class == "crypto" else "equity",
                    aliases=tuple(row.get("aliases", [])),
                    bare=bool(row.get("bare", True)),
                    product=row.get("product"),
                    legacy=tuple(row.get("legacy", [])),
                )
            )
    universe = {s.symbol for s in symbols}
    sp500 = config_dir / "sp500.csv"
    if sp500.exists():
        with sp500.open() as fh:
            universe |= {row["symbol"].replace(".", "-") for row in csv.DictReader(fh)}
            universe |= {row.replace("-", ".") for row in universe}
    stoplist = data.get("stoplist", [])
    bad = [w for w in stoplist if not isinstance(w, str)]
    if bad:  # YAML 1.1 reads bare ON/OFF/YES/NO as booleans
        raise ValueError(f"quote these stoplist entries in watchlist.yaml: {bad}")
    return Watchlist(symbols, frozenset(stoplist), frozenset(universe))
