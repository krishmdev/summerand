// Pure helpers used by the side panel. No DOM access here, so node --test can cover them.
(function (root) {
  "use strict";

  const COMPONENTS = [
    { key: "velocity", label: "Velocity", weight: 0.3 },
    { key: "credibility", label: "Credibility", weight: 0.2 },
    { key: "recency", label: "Recency", weight: 0.2 },
    { key: "market", label: "Market move", weight: 0.2 },
    { key: "novelty", label: "Novelty", weight: 0.1 },
  ];

  // Share of the final score each component contributes, using the same renormalization as the
  // server (components that are null drop out and the remaining weights are rescaled).
  function contributions(components) {
    const avail = COMPONENTS.filter((c) => components[c.key] !== null && components[c.key] !== undefined);
    const total = avail.reduce((s, c) => s + c.weight, 0) || 1;
    return COMPONENTS.map((c) => {
      const v = components[c.key];
      const missing = v === null || v === undefined;
      return {
        key: c.key,
        label: c.label,
        value: missing ? null : v,
        share: missing ? 0 : (c.weight * v) / total,
      };
    });
  }

  function safeHref(url) {
    try {
      const u = new URL(url);
      return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
    } catch {
      return null;
    }
  }

  function formatPct(pct) {
    if (pct === null || pct === undefined || Number.isNaN(pct)) return "–";
    const s = Math.abs(pct) < 0.05 ? "0.0" : Math.abs(pct).toFixed(1);
    return (pct > 0.05 ? "+" : pct < -0.05 ? "−" : "") + s + "%";
  }

  function direction(pct) {
    if (pct === null || pct === undefined || Math.abs(pct) < 0.05) return "flat";
    return pct > 0 ? "up" : "down";
  }

  function timeAgo(tsMs, nowMs) {
    const m = Math.max(0, Math.round((nowMs - tsMs) / 60000));
    if (m < 1) return "just now";
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    return h < 24 ? `${h}h ${m % 60}m ago` : `${Math.floor(h / 24)}d ago`;
  }

  function hhmmUTC(tsMs) {
    const d = new Date(tsMs);
    return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} UTC`;
  }

  // "Solana halted [1]. SOL fell [2][3]." -> text and citation segments, so the panel can turn
  // citations into links without ever parsing HTML.
  function briefSegments(text, sources) {
    const byN = new Map((sources || []).map((s) => [s.n, s]));
    const out = [];
    const re = /\[(\d+)\]/g;
    let last = 0;
    for (const m of text.matchAll(re)) {
      if (m.index > last) out.push({ text: text.slice(last, m.index) });
      const src = byN.get(Number(m[1]));
      out.push({ cite: Number(m[1]), href: src ? safeHref(src.url) : null, title: src ? src.title : "" });
      last = m.index + m[0].length;
    }
    if (last < text.length) out.push({ text: text.slice(last) });
    return out;
  }

  function splitWhy(text) {
    const lines = text.split("\n");
    const why = lines.filter((l) => /^why it matters:/i.test(l.trim()));
    const body = lines.filter((l) => !/^why it matters:/i.test(l.trim())).join(" ").trim();
    return { body, why: why.length ? why[0].replace(/^why it matters:\s*/i, "").trim() : "" };
  }

  function filterStories(stories, { tickers = [], pageTickers = null } = {}) {
    let out = stories;
    if (tickers.length) out = out.filter((s) => s.tickers.some((t) => tickers.includes(t)));
    if (pageTickers) out = out.filter((s) => s.tickers.some((t) => pageTickers.includes(t)));
    return out;
  }

  // What the content script needs for tooltips: best-ranked story and move per symbol.
  function tickerInfo(stories) {
    const info = {};
    for (const s of stories) {
      for (const t of s.tickers) {
        if (info[t]) continue;
        const move = (s.moves || []).find((m) => m.symbol === t);
        info[t] = { rank: s.rank, label: s.label, pct: move ? move.pct : null };
      }
    }
    return info;
  }

  root.SummerandView = {
    COMPONENTS,
    contributions,
    safeHref,
    formatPct,
    direction,
    timeAgo,
    hhmmUTC,
    briefSegments,
    splitWhy,
    filterStories,
    tickerInfo,
  };
})(globalThis);
