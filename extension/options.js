(function () {
  "use strict";
  const DEFAULTS = { serverUrl: "http://127.0.0.1:8000", highlight: true };
  const url = document.getElementById("serverUrl");
  const highlight = document.getElementById("highlight");
  const saved = document.getElementById("saved");

  chrome.storage.sync.get(DEFAULTS, (s) => {
    url.value = s.serverUrl;
    highlight.checked = s.highlight;
  });

  document.getElementById("form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    let origin;
    try {
      const u = new URL(url.value.trim());
      if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error("scheme");
      origin = `${u.protocol}//${u.hostname}/*`;
      url.value = u.origin;
    } catch {
      saved.textContent = "Enter an http(s) URL.";
      return;
    }
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
