// Ticker matching for page highlighting. Mirrors summerand/etl/tickers.py; both are tested
// against extension/tests/ticker_cases.json. Loaded as a classic script (content script and
// side panel), so it attaches to globalThis instead of exporting.
(function (root) {
  "use strict";

  const CASHTAG = /(?<![A-Za-z0-9$])\$([A-Z]{1,5}(?:\.[A-Z])?)(?![A-Za-z0-9])/g;
  const EXCHANGE =
    /\((?:NASDAQ|Nasdaq|NYSE American|NYSE Arca|NYSE|AMEX|Cboe|CBOE)\s*:\s*([A-Z][A-Z.]{0,5})\)/g;
  const BARE = /(?<![A-Za-z0-9$])[A-Z]{2,5}(?![A-Za-z0-9])/g;
  const POLYGON = /(?<![A-Za-z0-9])Polygon(?![A-Za-z0-9.])/g;
  const POL_CONTEXT =
    /\b(POL|MATIC|network|blockchain|layer[- ]?2|L2|zkEVM|AggLayer|token|chain|staking)\b/i;
  const CRYPTO_CONTEXT =
    /\b(crypto|cryptocurrency|token|tokens|blockchain|coin|coins|fees|staking|DeFi|stablecoin|exchange|wallet|on-chain|mainnet)\b/i;
  const MARKET_CONTEXT =
    /(\b(shares|stock|stocks|earnings|revenue|investors|market|markets|Nasdaq|NYSE|analyst|analysts|quarter|quarterly|guidance|valuation|trading|traders|rally|sell-off|index|profit|sales|CEO|deliveries|iPhone)\b|S&P|%|\$\d)/i;

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

    function isCrypto(sym) {
      return watch.has(sym) && watch.get(sym).asset_class === "crypto";
    }

    function contextOk(sym, text, found, alias) {
      const spec = watch.get(sym);
      if (!spec || !spec.context || alias.includes(" ")) return true;
      if (spec.context === "market") return MARKET_CONTEXT.test(text);
      return CRYPTO_CONTEXT.test(text) || found.some((m) => m.symbol !== sym && isCrypto(m.symbol));
    }

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
        if (!universe.has(m[1])) continue;
        const start = m.index + m[0].indexOf(m[1], m[0].indexOf(":"));
        add(start, start + m[1].length, m[1], "exchange");
      }
      // Ungated matches first, so they can supply crypto context for the gated ones.
      const gated = [];
      if (aliasRe) {
        for (const m of text.matchAll(aliasRe)) {
          const sym = aliasTo.get(m[1]);
          const spec = watch.get(sym);
          if (spec && spec.context && !m[1].includes(" ")) {
            gated.push([m.index, m.index + m[0].length, sym, "alias", m[1]]);
          } else add(m.index, m.index + m[0].length, sym, "alias");
        }
      }
      if (watch.has("POL") && POL_CONTEXT.test(text)) {
        for (const m of text.matchAll(POLYGON)) add(m.index, m.index + m[0].length, "POL", "alias");
      }
      if (!mostlyUpper(text)) {
        for (const m of text.matchAll(BARE)) {
          const word = m[0];
          if (stop.has(word)) continue;
          if (legacyTo.has(word)) {
            add(m.index, m.index + word.length, legacyTo.get(word), "legacy");
            continue;
          }
          const spec = watch.get(word);
          if (!spec || spec.bare === false) continue;
          const after = text.slice(m.index + word.length).trimStart();
          if ((spec.not_before || []).some((w) => after.startsWith(w))) continue;
          if (word === "POL" || spec.context) gated.push([m.index, m.index + word.length, word, "bare", word]);
          else add(m.index, m.index + word.length, word, "bare");
        }
      }
      for (const [start, end, sym, kind, alias] of gated) {
        const ok =
          sym === "POL"
            ? CRYPTO_CONTEXT.test(text) || found.some((m) => isCrypto(m.symbol))
            : contextOk(sym, text, found, alias);
        if (ok) add(start, end, sym, kind);
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
