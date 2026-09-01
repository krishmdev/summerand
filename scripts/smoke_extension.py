"""Load the unpacked extension in Chromium (Playwright) against a local demo server.

    uv run --group e2e python scripts/smoke_extension.py [--fixture ...] [--shots docs/]

Checks that the side panel renders ranked cards from the WebSocket, that a card expands to its
brief, that the error state appears when the server is gone, and that the content script
highlights tickers on an ordinary page. Everything talks to 127.0.0.1 only, so it runs under the
offline wrapper. Screenshots go to --shots when given.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"

ARTICLE = """<!doctype html><html><head><meta charset="utf-8"><title>Test article</title></head>
<body style="font:16px Georgia;max-width:640px;margin:40px auto">
<h1>Bitcoin slips as Nvidia (NASDAQ: NVDA) earnings loom</h1>
<p>Traders sold $ETH and Solana on Tuesday. The AI trade and the CEO commentary were in focus.</p>
<script>document.title = "ok"</script>
</body></html>"""


def wait_http(url: str, timeout: float = 900) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.suppress(OSError, ValueError), urllib.request.urlopen(url, timeout=2) as r:
            data = json.loads(r.read())
            if data.get("replay", {}).get("done"):
                return data
        time.sleep(1)
    raise TimeoutError(url)


def serve_article(port: int) -> http.server.ThreadingHTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = ARTICLE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default=str(ROOT / "fixtures" / "demo_feed.jsonl.gz"))
    ap.add_argument("--embedder", default="auto")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--shots", type=Path)
    ap.add_argument("--until", help="stop the replay here (ISO, UTC), e.g. during market hours")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    results: dict[str, object] = {}

    log_path = Path(tempfile.mkdtemp()) / "demo.log"
    demo_log = log_path.open("w")
    demo = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "summerand",
            "demo",
            "--speed",
            "0",
            "--port",
            str(args.port),
            "--fixture",
            args.fixture,
            "--embedder",
            args.embedder,
            *(["--until", args.until] if args.until else []),
            "--db",
            str(Path(tempfile.mkdtemp()) / "smoke.sqlite"),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "SUMMERAND_EGRESS_CHECK": os.environ.get("SUMMERAND_EGRESS_CHECK", "off"),
        },
        stdout=demo_log,
        stderr=subprocess.STDOUT,
    )
    article = serve_article(args.port + 1)
    try:
        status = wait_http(f"{base}/api/status")
        results["server"] = {k: status[k] for k in ("news", "clusters", "embedder_id")}
        with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
            ctx = p.chromium.launch_persistent_context(
                profile,
                channel="chromium",
                headless=True,
                args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
                viewport={"width": 380, "height": 900},
                device_scale_factor=2,
            )
            sw = (
                ctx.service_workers[0]
                if ctx.service_workers
                else ctx.wait_for_event("serviceworker")
            )
            ext_id = sw.url.split("/")[2]
            sw.evaluate(f"chrome.storage.sync.set({{serverUrl: '{base}'}})")

            page = ctx.new_page()
            page.goto(f"chrome-extension://{ext_id}/sidepanel.html")
            page.wait_for_selector(".card", timeout=30_000)
            page.wait_for_selector("#marketBrief:not([hidden])", timeout=30_000)
            results["cards"] = page.locator(".card").count()
            results["conn"] = page.locator("#conn").get_attribute("data-state")
            results["first_label"] = page.locator(".card .label").first.text_content()
            if args.shots:
                args.shots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(args.shots / "sidepanel.png"), animations="disabled")
            page.locator(".card details summary").first.click()
            results["expanded_brief"] = bool(
                page.locator(".card details[open] .sources li").count()
            )
            if args.shots:
                page.locator(".card").first.screenshot(
                    path=str(args.shots / "sidepanel-card.png"), animations="disabled"
                )
            page.set_viewport_size({"width": 480, "height": 900})
            results["wide_cards"] = page.locator(".card").count()

            art = ctx.new_page()
            art.goto(f"http://127.0.0.1:{args.port + 1}/")
            art.wait_for_selector("mark.smr-ticker", timeout=10_000)
            marks = art.locator("mark.smr-ticker").all_text_contents()
            results["highlighted"] = marks
            results["tooltip"] = art.locator("mark.smr-ticker").first.get_attribute("title")
            results["script_blocks"] = art.locator("mark.smr-ticker script").count()

            demo.send_signal(signal.SIGINT)
            demo.wait(timeout=20)
            page.wait_for_selector("#conn[data-state='offline']", timeout=30_000)
            results["disconnected_banner"] = page.locator(
                "#banner [role], #banner p"
            ).first.text_content()
            if args.shots:
                page.screenshot(
                    path=str(args.shots / "state-disconnected.png"), animations="disabled"
                )
            empty = ctx.new_page()
            empty.goto(f"chrome-extension://{ext_id}/sidepanel.html")
            empty.wait_for_selector(".state.error", timeout=30_000)
            results["error_state"] = empty.locator(".state.error h2").text_content()
            ctx.close()
    except Exception:
        print(log_path.read_text()[-3000:], file=sys.stderr)
        raise
    finally:
        if demo.poll() is None:
            demo.kill()
        article.shutdown()

    print(json.dumps(results, indent=2))
    ok = (
        results.get("cards", 0) > 0
        and results.get("conn") == "live"
        and results.get("expanded_brief")
        and set(results.get("highlighted", [])) >= {"Bitcoin", "NVDA", "$ETH", "Solana"}
        and "AI" not in results.get("highlighted", [])
        and results.get("error_state")
    )
    print("SMOKE", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
