// Highlights tickers on the page and reports which ones appear, for the panel's "On this page"
// filter. Only text nodes are read and only DOM APIs are used to wrap matches (no innerHTML):
// page text and RSS titles are untrusted.
(function () {
  "use strict";
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA", "INPUT", "SELECT", "CODE", "PRE", "MARK"]);
  const MAX_NODES = 4000;
  let matcher = SummerandTickers.compile(globalThis.SummerandDefaultWatchlist);
  let info = {};
  let enabled = true;
  const found = new Set();

  function tooltip(sym) {
    const i = info[sym];
    if (!i) return `${sym}: not in the current ranking`;
    const move = i.pct === null || i.pct === undefined ? "" : ` · ${i.pct > 0 ? "+" : ""}${i.pct.toFixed(1)}% since first report`;
    if (i.broad) return `${sym} · in broad mixed coverage (#${i.rank})${move}`;
    return `${sym} · story #${i.rank}: ${i.label}${move}`;
  }

  function decorate(mark) {
    const i = info[mark.dataset.symbol];
    mark.dataset.ranked = i ? "yes" : "no";
    if (i && i.pct) mark.dataset.dir = i.pct > 0 ? "up" : "down";
    else delete mark.dataset.dir;
    mark.title = tooltip(mark.dataset.symbol);
  }

  function skipNode(node) {
    for (let el = node.parentElement; el; el = el.parentElement) {
      if (SKIP.has(el.tagName) || el.isContentEditable) return true;
    }
    return false;
  }

  function wrap(node) {
    const text = node.nodeValue;
    if (!text || text.length < 2 || skipNode(node)) return;
    const matches = matcher.find(text);
    if (!matches.length) return;
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const m of matches) {
      found.add(m.symbol);
      if (m.start > last) frag.appendChild(document.createTextNode(text.slice(last, m.start)));
      const mark = document.createElement("mark");
      mark.className = "smr-ticker";
      mark.dataset.symbol = m.symbol;
      mark.textContent = text.slice(m.start, m.end);
      decorate(mark);
      frag.appendChild(mark);
      last = m.end;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }

  function scan(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode() && nodes.length < MAX_NODES) nodes.push(walker.currentNode);
    if (enabled) nodes.forEach(wrap);
    else for (const n of nodes) for (const m of matcher.find(n.nodeValue || "")) found.add(m.symbol);
  }

  function refreshTooltips() {
    for (const mark of document.querySelectorAll("mark.smr-ticker")) decorate(mark);
  }

  // Collect added nodes from every callback and scan them together after a short pause, so a
  // burst of mutations costs one pass and nothing that arrives mid-wait is missed.
  const added = new Set();
  let pending = null;
  const observer = new MutationObserver((records) => {
    for (const r of records) {
      for (const n of r.addedNodes) {
        if (n.nodeType === Node.ELEMENT_NODE && !n.matches("mark.smr-ticker")) added.add(n);
      }
    }
    if (pending || !added.size) return;
    pending = setTimeout(() => {
      pending = null;
      const batch = [...added].filter((n) => n.isConnected);
      added.clear();
      for (const n of batch) scan(n);
    }, 500);
  });

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg && msg.type === "summerand:pageTickers") reply({ tickers: [...found].sort(), url: location.href });
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.tickerInfo) {
      info = changes.tickerInfo.newValue || {};
      refreshTooltips();
    }
    if (changes.watchlist && changes.watchlist.newValue) {
      matcher = SummerandTickers.compile(changes.watchlist.newValue);
    }
  });

  chrome.storage.local.get(["tickerInfo", "watchlist"], (local) => {
    info = local.tickerInfo || {};
    if (local.watchlist) matcher = SummerandTickers.compile(local.watchlist);
    chrome.storage.sync.get({ highlight: true }, (opts) => {
      enabled = opts.highlight;
      scan(document.body);
      observer.observe(document.body, { childList: true, subtree: true });
    });
  });
})();
