import asyncio

import pytest
from fastapi.testclient import TestClient

from summerand.api.app import create_app
from summerand.api.ws import QUEUE_SIZE, Client, Hub
from summerand.nlp.embed import HashingEmbedder
from summerand.stack import build_stack, run_replay
from tests.conftest import ROOT


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    from summerand.config import Settings

    db = tmp_path_factory.mktemp("api") / "api.sqlite"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{db}",
        summerand_embedder="hashing",
        summerand_llm="off",
    )
    stack = build_stack(settings, chain=[HashingEmbedder()], llm=None)
    asyncio.run(run_replay(stack, ROOT / "fixtures/tiny_feed.jsonl", None))
    return stack


@pytest.fixture
def client(replayed):
    hub = Hub()
    app = create_app(replayed.store, hub, replayed.settings.watchlist, status=replayed.status)
    with TestClient(app) as c:
        yield c


def test_rest_endpoints(client):
    assert client.get("/healthz").json()["status"] == "ok"
    stories = client.get("/api/stories?limit=2").json()
    assert len(stories["stories"]) == 2 and stories["stale"] is False
    top = stories["stories"][0]
    assert {"cluster_id", "rank", "score", "components", "label", "tickers", "items"} <= set(top)
    detail = client.get(f"/api/clusters/{top['cluster_id']}").json()
    assert detail["articles"] and detail["embedder_id"].startswith("hashing/")
    assert client.get("/api/clusters/nope").status_code == 404
    sol = client.get("/api/stories?ticker=SOL").json()["stories"]
    assert sol and all("SOL" in s["tickers"] for s in sol)
    assert client.get("/api/briefs/latest").json()["market"]["kind"] == "market"
    bars = client.get("/api/tickers/BTC/bars").json()["bars"]
    assert len(bars) == 65 and bars[0]["ts_ms"] < bars[-1]["ts_ms"]
    assert client.get("/api/tickers/bad$sym/bars").status_code == 400
    arts = client.get("/api/articles?ticker=ETH").json()["articles"]
    assert arts and all("ETH" in a["tickers"] for a in arts)
    wl = client.get("/api/watchlist").json()
    assert any(s["symbol"] == "POL" for s in wl["symbols"]) and "AI" in wl["stoplist"]
    status = client.get("/api/status").json()
    assert status["late_dropped"] == {} and status["generation"] == 1


def test_cors_allows_the_extension(client):
    r = client.get(
        "/api/stories", headers={"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"}
    )
    assert r.headers["access-control-allow-origin"].startswith("chrome-extension://")
    r = client.get("/api/stories", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_websocket_sends_snapshot_and_honours_subscribe(client):
    with client.websocket_connect("/ws/stream") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and first["stories"]
        assert ws.receive_json()["type"] == "market_brief"
        ws.send_json({"type": "subscribe", "tickers": ["sol"]})
        filtered = ws.receive_json()
        assert filtered["filter"] == ["SOL"]
        assert filtered["stories"] and all("SOL" in s["tickers"] for s in filtered["stories"])


def test_websocket_rejects_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/stream", headers={"Origin": "https://evil.example"}) as ws,
    ):
        ws.receive_json()


async def test_slow_clients_are_dropped_not_buffered():
    hub = Hub()
    slow = Client(ws=None)
    fast = Client(ws=None)
    hub.clients |= {slow, fast}
    for i in range(QUEUE_SIZE + 1):
        hub.broadcast({"type": "heartbeat", "ts_ms": i})
        while not fast.queue.empty():
            fast.queue.get_nowait()
    assert slow.dropped and slow not in hub.clients
    assert fast in hub.clients and hub.dropped_clients == 1
