import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
require("../shared/watchlist.default.js");
require("../shared/tickers.js");

const cases = JSON.parse(readFileSync(new URL("./ticker_cases.json", import.meta.url), "utf8"));
const matcher = globalThis.SummerandTickers.compile(globalThis.SummerandDefaultWatchlist);

for (const c of cases.filter((c) => c.only !== "python")) {
  test(`shared case: ${c.text}`, () => {
    assert.deepEqual(matcher.extract(c.text), [...c.expect].sort());
  });
}

test("spans point at the matched text", () => {
  const text = "Shares of Nvidia and $AAPL rose";
  const got = matcher.find(text).map((m) => [text.slice(m.start, m.end), m.symbol]);
  assert.deepEqual(got, [["Nvidia", "NVDA"], ["$AAPL", "AAPL"]]);
});

test("exchange spans cover only the symbol", () => {
  const text = "Coinbase (NASDAQ: COIN) jumps";
  const m = matcher.find(text).find((x) => x.kind === "exchange");
  assert.equal(text.slice(m.start, m.end), "COIN");
});

test("an extra universe enables cashtags outside the watchlist", () => {
  const withSp = globalThis.SummerandTickers.compile(globalThis.SummerandDefaultWatchlist, ["INTC"]);
  assert.deepEqual(withSp.extract("$INTC jumps"), ["INTC"]);
  assert.deepEqual(matcher.extract("$INTC jumps"), []);
});
