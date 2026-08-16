from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from summerand import __version__
from summerand.api.ws import Hub
from summerand.config import Watchlist
from summerand.store.repo import Store

ORIGINS = r"^(chrome-extension://[a-z]{32}|https?://(localhost|127\.0\.0\.1)(:\d+)?)$"


def create_app(
    store: Store,
    hub: Hub,
    watchlist: Watchlist,
    status: Callable[[], dict[str, Any]] | None = None,
    lifespan: Any = None,
) -> FastAPI:
    app = FastAPI(title="Summerand", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origin_regex=ORIGINS, allow_methods=["GET"], allow_headers=["*"]
    )

    def latest_snapshot() -> dict[str, Any] | None:
        return hub.latest.get("snapshot") or store.latest_snapshot()

    if hub.fallback_snapshot is None:
        hub.fallback_snapshot = store.latest_snapshot

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        snap = latest_snapshot()
        return {"status": "ok", "event_ms": snap["ts_ms"] if snap else None}

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        base = status() if status else {}
        return {**base, "ws_clients": len(hub.clients), "ws_dropped": hub.dropped_clients}

    @app.get("/api/stories")
    def stories(limit: int = Query(20, ge=1, le=100), ticker: str | None = None) -> dict[str, Any]:
        snap = latest_snapshot()
        if snap is None:
            return {"ts_ms": None, "stale": False, "stories": []}
        items = snap["stories"]
        if ticker:
            items = [s for s in items if ticker.upper() in s["tickers"]]
        return {**snap, "stories": items[:limit]}

    @app.get("/api/clusters/{cluster_id}")
    def cluster(cluster_id: str) -> dict[str, Any]:
        found = store.cluster(cluster_id)
        if found is None:
            raise HTTPException(404, "unknown cluster")
        return found

    @app.get("/api/briefs/latest")
    def latest_brief() -> dict[str, Any]:
        return {"market": hub.latest.get("market_brief") or store.latest_market_brief()}

    @app.get("/api/tickers/{symbol}/bars")
    def bars(symbol: str, since: int = 0, limit: int = Query(500, ge=1, le=5000)) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9.\-]{1,12}", symbol):
            raise HTTPException(400, "bad symbol")
        return {"symbol": symbol.upper(), "bars": store.bars(symbol, since, limit)}

    @app.get("/api/articles")
    def articles(ticker: str | None = None, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        return {"articles": store.articles(ticker, limit)}

    @app.get("/api/watchlist")
    def get_watchlist() -> dict[str, Any]:
        return watchlist.to_json()

    @app.websocket("/ws/stream")
    async def stream(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if origin and not re.match(ORIGINS, origin):
            await ws.close(code=1008)
            return
        initial = []
        snap = latest_snapshot()
        if snap is not None:
            initial.append({**snap, "type": "snapshot"})
        market = hub.latest.get("market_brief") or store.latest_market_brief()
        if market is not None:
            initial.append({**market, "type": "market_brief"})
        await hub.serve(ws, initial)

    return app
