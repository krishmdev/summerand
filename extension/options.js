(function () {
  "use strict";
  const DEFAULTS = { serverUrl: "http://127.0.0.1:8000", highlight: true };
  const url = document.getElementById("serverUrl");
  const highlight = document.getElementById("highlight");
  const saved = document.getElementById("saved");
  const testResult = document.getElementById("testResult");

  function parse(value) {
    try {
      const u = new URL(value.trim());
      return u.protocol === "http:" || u.protocol === "https:" ? u : null;
    } catch {
      return null;
    }
  }

  function invalid(message) {
    url.setAttribute("aria-invalid", "true");
    testResult.textContent = "";
    saved.textContent = message;
  }

  url.addEventListener("input", () => url.removeAttribute("aria-invalid"));

  document.getElementById("test").addEventListener("click", async () => {
    const u = parse(url.value);
    if (!u) return invalid("Enter an http(s) URL, like http://127.0.0.1:8000.");
    testResult.textContent = "Checking…";
    try {
      const resp = await fetch(`${u.origin}/api/status`, { signal: AbortSignal.timeout(4000) });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const st = await resp.json();
      const embedder = st.embedder_id ? st.embedder_id.split("/")[1] : "unknown embedder";
      testResult.textContent = `Reachable: ${st.mode || "server"}, ${embedder}, ${st.clusters ?? "?"} clusters.`;
    } catch (err) {
      testResult.textContent = `No answer from ${u.origin} (${err.name === "TimeoutError" ? "timed out" : err.message}).`;
    }
  });

  chrome.storage.sync.get(DEFAULTS, (s) => {
    url.value = s.serverUrl;
    highlight.checked = s.highlight;
  });

  document.getElementById("form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const u = parse(url.value);
    if (!u) {
      invalid("Enter an http(s) URL, like http://127.0.0.1:8000.");
      return;
    }
    const origin = `${u.protocol}//${u.hostname}/*`;
    url.value = u.origin;
    const local = /^(localhost|127\.0\.0\.1)$/.test(new URL(url.value).hostname);
    if (!local && chrome.permissions) {
      const granted = await chrome.permissions.request({ origins: [origin] }).catch(() => false);
      if (!granted) {
        saved.textContent = "Chrome did not grant access to that host.";
        return;
      }
    }
    chrome.storage.sync.set({ serverUrl: url.value, highlight: highlight.checked }, () => {
      saved.textContent = "Saved.";
      setTimeout(() => (saved.textContent = ""), 2000);
    });
  });
})();
