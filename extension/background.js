// The service worker only wires the toolbar button to the side panel. The WebSocket lives in the
// panel page, which stays alive while it's open; an MV3 worker would be suspended.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});
