// Ticker matching for page highlighting. Mirrors summerand/etl/tickers.py; both are tested
// against tests/ticker_cases.json. Loaded as a classic script (content script and side panel),
// so it attaches to globalThis instead of exporting.
(function (root) {
  "use strict";

  const CASHTAG = /(?<![A-Za-z0-9$])\$([A-Z]{1,5}(?:\.[A-Z])?)(?![A-Za-z0-9])/g;
  const EXCHANGE =
    /\((?:NASDAQ|Nasdaq|NYSE American|NYSE Arca|NYSE|AMEX|Cboe|CBOE)\s*:\s*([A-Z][A-Z.]{0,5})\)/g;
  const BARE = /(?<![A-Za-z0-9$])[A-Z]{2,5}(?![A-Za-z0-9])/g;
  const POLYGON = /(?<![A-Za-z0-9])Polygon(?![A-Za-z0-9.])/g;
  const POL_CONTEXT =
    /\b(POL|MATIC|network|blockchain|layer[- ]?2|L2|zkEVM|AggLayer|token|chain|staking)\b/i;
  const CRYPTO_CONTEXT = /\b(crypto|token|tokens|blockchain|coin|fees|staking|DeFi)\b/i;

  function escapeRe(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function mostlyUpper(text) {
    const letters = text.match(/[A-Za-z]/g) || [];
    if (letters.length < 16) return false;
    return letters.filter((c) => c === c.toUpperCase()).length / letters.length > 0.6;
  }

  function compile(watchlist, extraUniverse) {
    const watch = new Map(watchlist.symbols.map((s) => [s.symbol, s]));
    const stop = new Set(watchlist.stoplist);
    const universe = new Set([...watch.keys(), ...(extraUniverse || [])]);
    const aliasTo = new Map();
    const legacyTo = new Map();
    for (const s of watchlist.symbols) {
      for (const a of s.aliases || []) aliasTo.set(a, s.symbol);
      for (const l of s.legacy || []) legacyTo.set(l, s.symbol);
    }
    const aliases = [...aliasTo.keys()].sort((a, b) => b.length - a.length);
    const aliasRe = aliases.length
      ? new RegExp(`(?<![A-Za-z0-9&])(${aliases.map(escapeRe).join("|")})(?![A-Za-z0-9&])`, "g")
      : null;

    function find(text) {
      const found = [];
      const add = (start, end, symbol, kind) => {
        if (found.some((m) => start < m.end && m.start < end)) return;
        found.push({ start, end, symbol, kind });
      };
      for (const m of text.matchAll(CASHTAG)) {
        const sym = legacyTo.get(m[1]) || m[1];
        if (universe.has(sym)) add(m.index, m.index + m[0].length, sym, "cashtag");
      }
      for (const m of text.matchAll(EXCHANGE)) {
        const start = m.index + m[0].indexOf(m[1], m[0].indexOf(":"));
        add(start, start + m[1].length, m[1], "exchange");
      }
      if (aliasRe) {
        for (const m of text.matchAll(aliasRe)) {
          add(m.index, m.index + m[0].length, aliasTo.get(m[1]), "alias");
        }
      }
      if (watch.has("POL") && POL_CONTEXT.test(text)) {
        for (const m of text.matchAll(POLYGON)) add(m.index, m.index + m[0].length, "POL", "alias");
      }
      if (!mostlyUpper(text)) {
        const cryptoContext =
          CRYPTO_CONTEXT.test(text) ||
          found.some((m) => watch.has(m.symbol) && watch.get(m.symbol).asset_class === "crypto");
        for (const m of text.matchAll(BARE)) {
          const word = m[0];
          if (stop.has(word)) continue;
          if (legacyTo.has(word)) {
            add(m.index, m.index + word.length, legacyTo.get(word), "legacy");
            continue;
          }
          const spec = watch.get(word);
          if (!spec || spec.bare === false) continue;
          if (word === "POL" && !cryptoContext) continue;
          add(m.index, m.index + word.length, word, "bare");
        }
      }
      return found.sort((a, b) => a.start - b.start);
    }

    function extract(text) {
      return [...new Set(find(text).map((m) => m.symbol))].sort();
    }

    return { find, extract };
  }

  root.SummerandTickers = { compile };
})(globalThis);
