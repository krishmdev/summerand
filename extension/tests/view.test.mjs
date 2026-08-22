import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
require("../shared/view.js");
const V = globalThis.SummerandView;

test("contributions renormalize when market is missing", () => {
  const parts = V.contributions({ velocity: 1, credibility: 0.5, recency: 1, market: null, novelty: 0 });
  const total = parts.reduce((s, p) => s + p.share, 0);
  assert.ok(Math.abs(total - 0.6 / 0.8) < 1e-9);
  assert.equal(parts.find((p) => p.key === "market").value, null);
});

test("safeHref only allows http(s)", () => {
  assert.equal(V.safeHref("https://example.com/a"), "https://example.com/a");
  assert.equal(V.safeHref("javascript:alert(1)"), null);
  assert.equal(V.safeHref("data:text/html,<b>x</b>"), null);
  assert.equal(V.safeHref("not a url"), null);
});

test("brief segments turn citations into links and never parse markup", () => {
  const segs = V.briefSegments("Solana halted <img src=x onerror=alert(1)> [1]. SOL fell [2][9].", [
    { n: 1, url: "https://a.example/1", title: "one" },
    { n: 2, url: "javascript:bad()", title: "two" },
  ]);
  assert.equal(segs[0].text, "Solana halted <img src=x onerror=alert(1)> ");
  assert.deepEqual(segs[1], { cite: 1, href: "https://a.example/1", title: "one" });
  assert.equal(segs.find((s) => s.cite === 2).href, null);
  assert.equal(segs.find((s) => s.cite === 9).href, null);
});

test("splitWhy separates the why-it-matters line", () => {
  assert.deepEqual(V.splitWhy("A [1]. B [2].\nWhy it matters: BTC -2.1% [1]."), {
    body: "A [1]. B [2].",
    why: "BTC -2.1% [1].",
  });
});

test("formatting", () => {
  assert.equal(V.formatPct(2.345), "+2.3%");
  assert.equal(V.formatPct(-0.51), "−0.5%");
  assert.equal(V.formatPct(0.01), "0.0%");
  assert.equal(V.formatPct(null), "–");
  assert.equal(V.direction(-1), "down");
  assert.equal(V.timeAgo(0, 90 * 60000), "1h 30m ago");
  assert.equal(V.hhmmUTC(Date.UTC(2026, 8, 22, 14, 5)), "14:05 UTC");
});

test("filters by ticker and by page", () => {
  const stories = [{ tickers: ["BTC"] }, { tickers: ["NVDA", "AAPL"] }, { tickers: [] }];
  assert.equal(V.filterStories(stories, { tickers: ["NVDA"] }).length, 1);
  assert.equal(V.filterStories(stories, { pageTickers: [] }).length, 0);
  assert.equal(V.filterStories(stories, {}).length, 3);
});

test("tickerInfo keeps the best-ranked story per symbol", () => {
  const info = V.tickerInfo([
    { rank: 1, label: "a", tickers: ["BTC"], moves: [{ symbol: "BTC", pct: -1.2 }] },
    { rank: 2, label: "b", tickers: ["BTC", "ETH"], moves: [] },
  ]);
  assert.deepEqual(info.BTC, { rank: 1, label: "a", pct: -1.2 });
  assert.deepEqual(info.ETH, { rank: 2, label: "b", pct: null });
});
