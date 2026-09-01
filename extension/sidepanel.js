// Side panel: holds the WebSocket to the Summerand server and renders the ranking.
// Everything is built with createElement/textContent. Story text comes from RSS feeds, so it is
// never parsed as HTML.
(function () {
  "use strict";
  const V = globalThis.SummerandView;
  const DEFAULT_SERVER = "http://127.0.0.1:8000";
  const MAX_CHIPS = 5;
  const hasChrome = Boolean(globalThis.chrome && chrome.storage && chrome.storage.sync);

  const state = {
    server: DEFAULT_SERVER,
    stories: [],
    snapshot: null,
    market: null,
    status: null,
    conn: "connecting",
    lastMessageAt: 0,
    lastSnapshotAt: 0,
    everConnected: false,
    error: null,
    tickerFilter: [],
    pageTickers: null,
    onPage: false,
    retry: 0,
    switching: false,
    expandedChips: new Set(),
  };
  let ws = null;
  let reconnectTimer = null;
  const seenClusters = new Set(); // only cards for newly surfaced clusters animate in

  const $ = (id) => document.getElementById(id);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

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

  function base() {
    return state.server.replace(/\/$/, "");
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

  const CONN_TEXT = {
    connecting: "Connecting",
    waiting: "Connected · waiting",
    live: "Connected",
    stale: "Stale",
    offline: "Offline",
  };

  function setConn(conn, text) {
    state.conn = conn;
    const el = $("conn");
    el.dataset.state = conn;
    el.querySelector(".conn-text").textContent = text || CONN_TEXT[conn];
  }

  function syncConn() {
    if (state.conn === "offline" || state.conn === "connecting") return;
    if (!state.snapshot) setConn("waiting");
    else if (state.snapshot.stale) setConn("stale");
    else setConn("live");
  }

  function connect() {
    clearTimeout(reconnectTimer);
    const url = base().replace(/^http/, "ws") + "/ws/stream";
    setConn("connecting", state.everConnected ? "Reconnecting" : "Connecting");
    try {
      ws = new WebSocket(url);
    } catch {
      fail(`${state.server} is not a valid server address`);
      return;
    }
    ws.addEventListener("open", () => {
      state.retry = 0;
      state.everConnected = true;
      state.error = null;
      setConn("waiting");
      syncConn();
      fetchWatchlist();
      fetchStatus();
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
      if (state.switching) {
        // The user picked a new server: this close is expected, so go straight to it.
        state.switching = false;
        connect();
        return;
      }
      state.retry += 1;
      const delay = Math.min(30000, 1000 * 2 ** Math.min(state.retry, 5));
      if (!state.everConnected || !state.snapshot) fail(`Nothing is answering at ${state.server}`);
      else {
        setConn("offline");
        render();
      }
      reconnectTimer = setTimeout(connect, delay);
    });
  }

  function retryNow() {
    state.retry = 0;
    if (ws) ws.close();
    else connect();
  }

  function fail(message) {
    state.error = message;
    setConn("offline");
    render();
  }

  async function fetchJSON(path) {
    const resp = await fetch(base() + path);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  async function fetchWatchlist() {
    try {
      saveLocal({ watchlist: await fetchJSON("/api/watchlist") });
    } catch {
      /* the bundled default stays in use */
    }
  }

  async function fetchStatus() {
    try {
      state.status = await fetchJSON("/api/status");
    } catch {
      return;
    }
    renderFooter();
    renderBrief();
  }

  function onMessage(msg) {
    if (msg.type === "snapshot") {
      state.snapshot = msg;
      state.stories = msg.stories || [];
      state.lastSnapshotAt = Date.now();
      saveLocal({ tickerInfo: V.tickerInfo(state.stories, msg.window_size) });
      syncConn();
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
      fetchStatus();
    } else if (msg.type === "heartbeat") {
      syncConn();
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

  function llmNote(method) {
    const llm = state.status && state.status.llm;
    if (method === "llm") return `written by ${llm || "an LLM"}, checked against the sources`;
    if (llm && llm.startsWith("disabled")) return `extractive · LLM off (${llm.replace(/^disabled \(|\)$/g, "")})`;
    return "extractive: sentences quoted from the cited stories";
  }

  function renderBrief() {
    const m = state.market;
    $("marketBrief").hidden = !m;
    if (!m) return;
    const { body } = V.splitWhy(m.text);
    // The extractive market brief quotes the top headlines; when every one of them is already a
    // card below, repeating them adds nothing, so only the price moves are shown.
    const headlineIds = new Set(state.stories.map((s) => s.headline.id));
    const redundant = (m.sources || []).length > 0 && m.sources.every((s) => headlineIds.has(s.id));
    if (redundant) $("briefBody").replaceChildren();
    else renderBriefText($("briefBody"), body, m.sources);

    const movers = V.parseMovers(m.facts);
    const grid = $("movers");
    grid.replaceChildren();
    if (movers.length) {
      grid.append(h("div", { class: "row", role: "row" },
        h("span", { class: "hdr", role: "columnheader" }, h("span", { class: "sr-only", text: "Symbol" })),
        h("span", { class: "hdr val", role: "columnheader", text: "1h" }),
        h("span", { class: "hdr val", role: "columnheader", text: "6h" })));
      for (const mv of movers) {
        grid.append(h("div", { class: "row", role: "row" },
          h("span", { class: "sym", role: "rowheader", text: mv.symbol }),
          h("span", { class: "val", role: "cell", dataset: { dir: V.direction(mv.h1) }, text: V.formatPct(mv.h1) }),
          h("span", { class: "val", role: "cell", dataset: { dir: V.direction(mv.h6) }, text: V.formatPct(mv.h6) })));
      }
    }
    $("briefMeta").textContent = `${V.hhmmUTC(m.ts_ms)} · ${llmNote(m.method)}`;
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

  function renderFooter() {
    const parts = [state.server.replace(/^https?:\/\//, "")];
    const st = state.status;
    if (st && st.embedder_id) parts.push(st.embedder_id.split("/")[1]);
    if (st && st.degraded) parts.push("embeddings degraded");
    $("server").textContent = parts.join(" · ");
  }

  function scoreBar(story) {
    const parts = V.contributions(story.components);
    const raw = parts.reduce((s, p) => s + p.share, 0) || 1;
    const shown = Math.round(story.score * 100);
    const desc = parts.map((p) => `${p.label} ${p.value === null ? "n/a" : Math.round(p.value * 100)}`).join(", ");
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
    const expanded = state.expandedChips.has(story.cluster_id);
    const shown = expanded ? story.tickers : story.tickers.slice(0, MAX_CHIPS);
    const chips = shown.map((sym) => {
      const m = moves.get(sym);
      const dir = m ? V.direction(m.pct) : "flat";
      const spoken = m ? `, ${dir === "up" ? "up" : dir === "down" ? "down" : "flat"} ${V.formatPct(Math.abs(m.pct))}` : "";
      return h("button", {
          type: "button", class: "tick", dataset: { dir },
          "aria-label": `Filter to ${sym}${spoken}`,
          title: m ? `${m.text}. Click to filter to ${sym}.` : `No price data for ${sym}. Click to filter.`,
          onclick: () => { state.tickerFilter = [sym]; render(); },
        },
        sym,
        m ? h("span", { class: "move", "aria-hidden": "true" },
              h("span", { class: "arrow", text: dir === "up" ? "▲" : dir === "down" ? "▼" : "■" }),
              " ", V.formatPct(m.pct)) : null);
    });
    const extra = story.tickers.length - MAX_CHIPS;
    if (extra > 0) {
      chips.push(h("button", {
        type: "button", class: "more-tickers", "aria-expanded": String(expanded),
        text: expanded ? "fewer" : `+${extra} more`,
        onclick: () => {
          if (expanded) state.expandedChips.delete(story.cluster_id);
          else state.expandedChips.add(story.cluster_id);
          render();
        },
      }));
    }
    return h("div", { class: "tickers" }, chips);
  }

  function sourcesList(story, broad) {
    if (story.brief && !broad) {
      const cited = new Set(V.briefSegments(story.brief.text, story.brief.sources).filter((s) => s.cite).map((s) => s.cite));
      return h("ol", { class: "sources" },
        story.brief.sources.map((it) =>
          h("li", { value: it.n, class: cited.has(it.n) ? null : "uncited" },
            link(it.url, it.title), " ", h("span", { class: "src", text: it.source }))));
    }
    return h("ol", { class: "sources" },
      story.items.slice(0, 5).map((it) =>
        h("li", {}, link(it.url, it.title), " ",
          h("span", { class: "src", text: `${it.source} · ${V.hhmmUTC(it.published_ms)}` }))));
  }

  function details(story, broad) {
    const title = broad ? "Top headlines" : story.brief ? "Brief and sources" : "Sources and score";
    const d = h("details", { class: "more" }, h("summary", { text: title }));
    if (story.brief && !broad) {
      const { body, why } = V.splitWhy(story.brief.text);
      const p = h("p", { class: "story-brief" });
      renderBriefText(p, body, story.brief.sources);
      d.appendChild(p);
      if (why) {
        const w = h("p", { class: "story-why" });
        renderBriefText(w, why, story.brief.sources);
        d.appendChild(w);
      }
      d.appendChild(h("p", { class: "brief-asof", text: `brief as of ${V.hhmmUTC(story.brief.ts_ms)}; the cluster may have grown since` }));
    }
    d.appendChild(sourcesList(story, broad));
    const dl = h("dl", { class: "breakdown", "aria-label": "Score components (0-100)" });
    for (const p of V.contributions(story.components)) {
      dl.appendChild(h("dt", {}, h("span", { class: "swatch", style: { background: `var(--c-${p.key})` }, "aria-hidden": "true" }), p.label));
      dl.appendChild(h("dd", { class: p.value === null ? "note" : null, text: p.value === null ? "no price data" : String(Math.round(p.value * 100)) }));
    }
    d.appendChild(dl);
    return d;
  }

  function card(story, snap) {
    const fresh = !seenClusters.has(story.cluster_id);
    seenClusters.add(story.cluster_id);
    const broad = V.isBroad(story, snap.window_size);
    const cls = ["card", fresh ? "new" : "", broad ? "broad" : ""].filter(Boolean).join(" ");
    const meta = `${plural(story.size, "report")} · ${plural(story.sources.length, "source")} · last ${V.timeAgo(story.last_ms, snap.ts_ms)}`;
    const heading = broad
      ? h("h3", { class: "headline" }, h("span", { class: "sr-only", text: `Rank ${story.rank}: ` }),
          `${story.size} of ${snap.window_size} articles, ${story.sources.length} sources`)
      : h("h3", { class: "headline" }, h("span", { class: "sr-only", text: `Rank ${story.rank}: ` }),
          link(story.headline.url, story.headline.title));
    return h("li", { class: cls, dataset: { rank: story.rank, id: story.cluster_id } },
      h("div", { class: "rank", "aria-hidden": "true", text: String(story.rank) }),
      h("div", { class: "card-body" },
        h("p", { class: "label", text: broad ? "Broad · mixed coverage" : story.label || "untitled cluster" }),
        heading,
        broad
          ? h("ul", { class: "digest" }, story.items.slice(0, 3).map((it) => h("li", {}, link(it.url, it.title))))
          : h("p", { class: "meta", text: meta }),
        broad ? h("p", { class: "meta", text: `last ${V.timeAgo(story.last_ms, snap.ts_ms)}` }) : null,
        scoreBar(story),
        story.tickers.length ? tickerChips(story) : null,
        details(story, broad)));
  }

  function renderState(kind, title, lines, action) {
    const box = h("div", { class: `state ${kind}` }, h("h2", { text: title }), lines.map((l) => h("p", {}, ...l)));
    if (action) box.appendChild(action);
    $("state").replaceChildren(box);
  }

  function secondsAgo(ts) {
    const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
    return s < 90 ? `${s}s ago` : `${Math.round(s / 60)} min ago`;
  }

  function renderBanner(snap) {
    const banner = $("banner");
    const kind = state.conn === "offline" && snap ? "offline" : snap && snap.stale ? "stale" : "";
    if (kind === "offline" && banner.dataset.kind === "offline") {
      updateBannerAge();
      return; // keep the alert node as-is so screen readers announce it once
    }
    banner.dataset.kind = kind;
    banner.replaceChildren();
    banner.removeAttribute("role");
    if (kind === "offline") {
      banner.setAttribute("role", "alert");
      banner.appendChild(h("p", { class: "banner" },
        "Disconnected. Showing the last ranking.",
        h("span", { class: "banner-age", "aria-hidden": "true" }),
        h("button", { type: "button", class: "btn", text: "Retry now", onclick: retryNow })));
      updateBannerAge();
    } else if (kind === "stale") {
      banner.setAttribute("role", "status");
      banner.appendChild(h("p", { class: "banner",
        text: `Embeddings are unavailable, so this is the ranking from ${V.hhmmUTC(snap.as_of_ms)}. It updates again once they recover.` }));
    }
  }

  function updateBannerAge() {
    const age = document.querySelector("#banner .banner-age");
    if (age) age.textContent = ` Received ${secondsAgo(state.lastSnapshotAt)}.`;
  }

  function render() {
    renderLegend();
    renderFooter();
    const main = $("main");
    const list = $("stories");
    const snap = state.snapshot;
    const hasStories = Boolean(snap && state.stories.length);
    $("legend").hidden = !hasStories;
    $("onpage").hidden = !hasStories;

    const filter = $("activeFilter");
    filter.replaceChildren();
    filter.hidden = !state.tickerFilter.length;
    if (state.tickerFilter.length) {
      filter.append(
        h("span", { class: "hint", text: "Filtered to" }),
        h("button", { type: "button", class: "chip-filter", title: "Clear ticker filter",
          "aria-label": `Clear filter ${state.tickerFilter.join(", ")}`,
          text: state.tickerFilter.join(","), onclick: () => { state.tickerFilter = []; render(); } })
      );
    }

    renderBanner(snap);
    const dim = Boolean(snap && (snap.stale || state.conn === "offline"));
    list.classList.toggle("is-stale", dim);
    $("marketBrief").classList.toggle("is-stale", dim);

    if (snap) {
      const asOf = snap.stale ? snap.as_of_ms : snap.ts_ms;
      $("asof").title = `Ranking as of ${V.hhmmUTC(asOf)}; ${snap.window_size} articles in the 6-hour window`;
      $("asof").replaceChildren(h("strong", { text: V.hhmmUTC(asOf) }),
        ` · ${plural(snap.window_size, "article")}, 6h`);
    }

    const foot = $("footnote");
    foot.hidden = true;
    if (state.error && !snap) {
      main.setAttribute("aria-busy", "false");
      list.replaceChildren();
      renderState("error", "Server not reachable", [
        [`${state.error}.`],
        ["Start it with ", h("code", { text: "make demo-hashing" }), " (or ", h("code", { text: "summerand demo" }),
         "), or change the address in Settings."],
      ], h("button", { type: "button", class: "btn", text: "Retry now", onclick: retryNow }));
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
    list.replaceChildren(...visible.map((s) => card(s, snap)));
    if (hasStories && state.stories.length <= 3 && !state.tickerFilter.length && !state.onPage) {
      foot.hidden = false;
      foot.textContent = `${plural(state.stories.length, "topic")} across ${snap.window_size} articles in the last 6h. ` +
        "Coverage is mixed, so stories are grouped broadly.";
    }
    if (!state.stories.length) {
      renderState("empty", "No stories yet", [["Clustering the first articles. A ranking appears every 30 seconds."]]);
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
  $("settings").addEventListener("click", () => {
    if (hasChrome && chrome.runtime && chrome.runtime.openOptionsPage) chrome.runtime.openOptionsPage();
    else window.open("options.html", "_blank");
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
        state.status = null;
        state.everConnected = false;
        state.error = null;
        state.retry = 0;
        clearTimeout(reconnectTimer);
        render();
        if (ws) {
          state.switching = true;
          ws.close();
        } else connect();
      }
    });
  }
  setInterval(() => {
    if (state.conn === "live" && Date.now() - state.lastMessageAt > 40000) setConn("live", "Connected · quiet");
    if (state.conn === "offline" && state.snapshot) updateBannerAge();
  }, 5000);

  render();
  loadSettings().then((server) => {
    state.server = server;
    connect();
  });
})();
