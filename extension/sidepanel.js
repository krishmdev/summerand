// Side panel: holds the WebSocket to the Summerand server and renders the ranking.
// Everything is built with createElement/textContent. Story text comes from RSS feeds, so it is
// never parsed as HTML.
(function () {
  "use strict";
  const V = globalThis.SummerandView;
  const DEFAULT_SERVER = "http://127.0.0.1:8000";
  const hasChrome = Boolean(globalThis.chrome && chrome.storage && chrome.storage.sync);

  const state = {
    server: DEFAULT_SERVER,
    stories: [],
    snapshot: null,
    market: null,
    conn: "connecting",
    lastMessageAt: 0,
    everConnected: false,
    error: null,
    tickerFilter: [],
    pageTickers: null,
    onPage: false,
    retry: 0,
  };
  let ws = null;
  const seenClusters = new Set(); // only cards for newly surfaced clusters animate in
  let reconnectTimer = null;

  const $ = (id) => document.getElementById(id);

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k === "style") Object.assign(el.style, v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return el;
  }

  function link(href, text, cls) {
    const safe = V.safeHref(href);
    if (!safe) return h("span", { class: cls, text });
    return h("a", { class: cls, href: safe, target: "_blank", rel: "noopener noreferrer", text });
  }

  // settings --------------------------------------------------------------------------------

  function loadSettings() {
    return new Promise((resolve) => {
      if (hasChrome) chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER }, (s) => resolve(s.serverUrl));
      else resolve(new URLSearchParams(location.search).get("server") || localStorage.getItem("serverUrl") || DEFAULT_SERVER);
    });
  }

  function saveLocal(values) {
    if (hasChrome) chrome.storage.local.set(values);
  }

  // connection ------------------------------------------------------------------------------

  function setConn(conn, text) {
    state.conn = conn;
    const el = $("conn");
    el.dataset.state = conn;
    el.querySelector(".conn-text").textContent = text;
  }

  function connect() {
    clearTimeout(reconnectTimer);
    const url = state.server.replace(/^http/, "ws").replace(/\/$/, "") + "/ws/stream";
    setConn("connecting", state.everConnected ? "Reconnecting" : "Connecting");
    try {
      ws = new WebSocket(url);
    } catch (err) {
      fail(`Bad server URL: ${state.server}`);
      return;
    }
    ws.addEventListener("open", () => {
      state.retry = 0;
      state.everConnected = true;
      state.error = null;
      setConn("live", "Live");
      fetchWatchlist();
      render();
    });
    ws.addEventListener("message", (ev) => {
      state.lastMessageAt = Date.now();
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      onMessage(msg);
    });
    ws.addEventListener("close", () => {
      ws = null;
      state.retry += 1;
      const delay = Math.min(30000, 1000 * 2 ** Math.min(state.retry, 5));
      if (!state.everConnected || !state.snapshot) {
        fail(`Can't reach ${state.server}`);
      } else {
        setConn("offline", "Offline");
        render();
      }
      reconnectTimer = setTimeout(connect, delay);
    });
  }

  function fail(message) {
    state.error = message;
    setConn("offline", "Offline");
    render();
  }

  async function fetchWatchlist() {
    try {
      const resp = await fetch(state.server.replace(/\/$/, "") + "/api/watchlist");
      if (resp.ok) saveLocal({ watchlist: await resp.json() });
    } catch {
      /* the bundled default stays in use */
    }
  }

  function onMessage(msg) {
    if (msg.type === "snapshot") {
      state.snapshot = msg;
      state.stories = msg.stories || [];
      saveLocal({ tickerInfo: V.tickerInfo(state.stories) });
      render();
    } else if (msg.type === "cluster_update" && msg.story) {
      const i = state.stories.findIndex((s) => s.cluster_id === msg.story.cluster_id);
      if (i >= 0) {
        state.stories[i] = { ...state.stories[i], brief: msg.story.brief };
        render();
      }
    } else if (msg.type === "market_brief") {
      state.market = msg;
      renderBrief();
    } else if (msg.type === "heartbeat" && state.conn !== "live") {
      setConn("live", "Live");
    }
  }

  // "On this page" --------------------------------------------------------------------------

  async function refreshPageTickers() {
    if (!state.onPage) return;
    if (!hasChrome || !chrome.tabs) {
      state.pageTickers = [];
      render();
      return;
    }
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const reply = tab ? await chrome.tabs.sendMessage(tab.id, { type: "summerand:pageTickers" }) : null;
      state.pageTickers = reply ? reply.tickers : [];
    } catch {
      state.pageTickers = []; // chrome:// pages and the Web Store don't run content scripts
    }
    render();
  }

  // rendering -------------------------------------------------------------------------------

  function renderBriefText(container, text, sources) {
    container.replaceChildren();
    for (const seg of V.briefSegments(text, sources)) {
      if (seg.text !== undefined) container.appendChild(document.createTextNode(seg.text));
      else if (seg.href) {
        container.appendChild(
          h("a", { class: "cite", href: seg.href, target: "_blank", rel: "noopener noreferrer",
                   title: seg.title, "aria-label": `source ${seg.cite}: ${seg.title}`, text: `${seg.cite}` })
        );
      } else container.appendChild(h("span", { class: "cite", text: `${seg.cite}` }));
    }
  }

  function renderBrief() {
    const m = state.market;
    $("marketBrief").hidden = !m;
    if (!m) return;
    const { body, why } = V.splitWhy(m.text);
    renderBriefText($("briefBody"), body, m.sources);
    renderBriefText($("briefWhy"), why, m.sources);
    $("briefMeta").textContent = `${V.hhmmUTC(m.ts_ms)} · ${m.method === "llm" ? "written by an LLM, checked against the sources" : "extractive: sentences quoted from the cited stories"}`;
  }

  function renderLegend() {
    const legend = $("legend");
    if (legend.childElementCount) return;
    legend.appendChild(h("span", { text: "Score:" }));
    for (const c of V.COMPONENTS) {
      legend.appendChild(
        h("span", { class: "legend-item" },
          h("span", { class: "swatch", style: { background: `var(--c-${c.key})` }, "aria-hidden": "true" }),
          c.label)
      );
    }
  }

  function scoreBar(story) {
    const parts = V.contributions(story.components);
    const raw = parts.reduce((s, p) => s + p.share, 0) || 1;
    const shown = Math.round(story.score * 100);
    const desc = parts
      .map((p) => `${p.label} ${p.value === null ? "n/a" : Math.round(p.value * 100)}`)
      .join(", ");
    const bar = h("div", { class: "bar", role: "img", "aria-label": `Score ${shown}. ${desc}` });
    for (const p of parts) {
      if (!p.share) continue;
      bar.appendChild(
        h("span", {
          class: "seg",
          style: { width: `${((p.share / raw) * story.score * 100).toFixed(2)}%`, background: `var(--c-${p.key})` },
          title: `${p.label}: ${Math.round(p.value * 100)}/100 (adds ${Math.round((p.share / raw) * story.score * 100)} points)`,
        })
      );
    }
    return h("div", { class: "score" }, bar, h("span", { class: "score-value", text: String(shown) }));
  }

  function tickerChips(story) {
    const moves = new Map((story.moves || []).map((m) => [m.symbol, m]));
    return h("div", { class: "tickers" },
      story.tickers.slice(0, 6).map((sym) => {
        const m = moves.get(sym);
        const dir = m ? V.direction(m.pct) : "flat";
        return h("button", {
            type: "button", class: "tick", dataset: { dir },
            title: m ? `${m.text}. Click to filter to ${sym}.` : `No price data for ${sym}. Click to filter.`,
            onclick: () => { state.tickerFilter = [sym]; render(); },
          },
          sym,
          m ? h("span", { class: "move" },
                h("span", { class: "arrow", "aria-hidden": "true", text: dir === "up" ? "▲" : dir === "down" ? "▼" : "■" }),
                " ", V.formatPct(m.pct)) : null);
      }));
  }

  function details(story) {
    const d = h("details", { class: "more" }, h("summary", { text: story.brief ? "Brief and sources" : "Sources and score" }));
    if (story.brief) {
      const { body, why } = V.splitWhy(story.brief.text);
      const p = h("p", { class: "story-brief" });
      renderBriefText(p, body, story.brief.sources);
      d.appendChild(p);
      if (why) {
        const w = h("p", { class: "story-why" });
        renderBriefText(w, why, story.brief.sources);
        d.appendChild(w);
      }
    }
    // With a brief, list exactly the items it cites, numbered the same way; otherwise the
    // three most recent reports.
    const listed = story.brief
      ? story.brief.sources.map((s) => ({ ...s, published_ms: null }))
      : story.items.slice(0, 3).map((it, i) => ({ ...it, n: i + 1 }));
    d.appendChild(
      h("ol", { class: "sources" },
        listed.map((it) =>
          h("li", { value: it.n }, link(it.url, it.title), " ",
            h("span", { class: "src", text: it.published_ms ? `${it.source} · ${V.hhmmUTC(it.published_ms)}` : it.source }))))
    );
    const dl = h("dl", { class: "breakdown", "aria-label": "Score components (0-100)" });
    for (const p of V.contributions(story.components)) {
      dl.appendChild(h("dt", {}, h("span", { class: "swatch", style: { background: `var(--c-${p.key})` } }), p.label));
      dl.appendChild(h("dd", { class: p.value === null ? "note" : null, text: p.value === null ? "no price data" : String(Math.round(p.value * 100)) }));
    }
    d.appendChild(dl);
    return d;
  }

  function card(story, nowMs) {
    const fresh = !seenClusters.has(story.cluster_id);
    seenClusters.add(story.cluster_id);
    const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
    return h("li", { class: fresh ? "card new" : "card", dataset: { rank: story.rank, id: story.cluster_id } },
      h("div", { class: "rank", "aria-label": `rank ${story.rank}`, text: String(story.rank) }),
      h("div", { class: "card-body" },
        h("p", { class: "label", text: story.label || "untitled cluster" }),
        h("h3", { class: "headline" }, link(story.headline.url, story.headline.title)),
        h("p", { class: "meta", text: `${plural(story.size, "report")} · ${plural(story.sources.length, "source")} · last ${V.timeAgo(story.last_ms, nowMs)}` }),
        scoreBar(story),
        story.tickers.length ? tickerChips(story) : null,
        details(story)));
  }

  function renderState(kind, title, lines, action) {
    const box = h("div", { class: `state ${kind}` }, h("h2", { text: title }), lines.map((l) => h("p", {}, ...l)));
    if (action) box.appendChild(action);
    $("state").replaceChildren(box);
  }

  function render() {
    renderLegend();
    $("legend").hidden = !state.snapshot;
    const main = $("main");
    const list = $("stories");
    const snap = state.snapshot;
    $("server").textContent = state.server.replace(/^https?:\/\//, "");

    const filter = $("activeFilter");
    filter.replaceChildren();
    if (state.tickerFilter.length) {
      filter.appendChild(h("button", { type: "button", class: "chip-filter", title: "Clear ticker filter",
        text: state.tickerFilter.join(","), onclick: () => { state.tickerFilter = []; render(); } }));
    }

    $("banner").replaceChildren();
    if (snap && snap.stale) {
      $("banner").appendChild(h("p", { class: "banner",
        text: `Embeddings are unavailable; showing the ranking from ${V.hhmmUTC(snap.as_of_ms)}.` }));
    } else if (state.conn === "offline" && snap) {
      $("banner").appendChild(h("p", { class: "banner", text: "Disconnected. Showing the last ranking received; retrying." }));
    }

    if (snap) {
      $("asof").replaceChildren("as of ", h("strong", { text: V.hhmmUTC(snap.ts_ms) }), ` · ${snap.window_size} in window`);
    }

    if (state.error && !snap) {
      main.setAttribute("aria-busy", "false");
      list.replaceChildren();
      renderState("error", "Can't reach the Summerand server", [
        [state.error + "."],
        ["Start it with ", h("code", { text: "summerand demo" }), " or change the address in Settings."],
      ], h("button", { type: "button", class: "btn", text: "Retry now", onclick: () => { state.retry = 0; connect(); } }));
      return;
    }
    if (!snap) {
      main.setAttribute("aria-busy", "true");
      $("state").replaceChildren();
      list.replaceChildren(...[0, 1, 2].map(() => h("li", { class: "skeleton", "aria-hidden": "true" })));
      return;
    }
    main.setAttribute("aria-busy", "false");
    const visible = V.filterStories(state.stories, {
      tickers: state.tickerFilter,
      pageTickers: state.onPage ? state.pageTickers || [] : null,
    });
    const nowMs = snap.ts_ms;
    list.replaceChildren(...visible.map((s) => card(s, nowMs)));
    if (!state.stories.length) {
      renderState("empty", "No stories yet", [["The pipeline publishes a ranking every 30 seconds of event time once enough articles have clustered."]]);
    } else if (!visible.length) {
      const why = state.onPage
        ? (state.pageTickers && state.pageTickers.length
            ? [["None of the tickers on this page (", h("code", { text: state.pageTickers.join(" ") }), ") are in a ranked story."]]
            : [["No watchlist tickers found on this page."]])
        : [["No ranked story mentions ", h("code", { text: state.tickerFilter.join(", ") }), "."]];
      renderState("empty", "Nothing matches", why);
    } else {
      $("state").replaceChildren();
    }
  }

  // wiring ----------------------------------------------------------------------------------

  $("onpage").addEventListener("click", () => {
    state.onPage = !state.onPage;
    $("onpage").setAttribute("aria-pressed", String(state.onPage));
    if (state.onPage) refreshPageTickers();
    else render();
  });
  if (hasChrome && chrome.tabs) {
    chrome.tabs.onActivated.addListener(refreshPageTickers);
    chrome.tabs.onUpdated.addListener((_id, info) => { if (info.status === "complete") refreshPageTickers(); });
  }
  if (hasChrome) {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === "sync" && changes.serverUrl) {
        state.server = changes.serverUrl.newValue || DEFAULT_SERVER;
        state.snapshot = null;
        state.market = null;
        if (ws) ws.close();
        else connect();
      }
    });
  }
  setInterval(() => {
    if (state.conn === "live" && Date.now() - state.lastMessageAt > 40000) setConn("connecting", "Quiet");
  }, 5000);

  render();
  loadSettings().then((server) => {
    state.server = server;
    connect();
  });
})();
