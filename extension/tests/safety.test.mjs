import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname;
const files = ["background.js", "content.js", "sidepanel.js", "options.js", "shared/tickers.js", "shared/view.js"];

test("no HTML-string sinks anywhere in the extension", () => {
  const sinks = /\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write|\beval\(|new Function\(/;
  for (const f of files) {
    const src = readFileSync(join(root, f), "utf8");
    assert.ok(!sinks.test(src), `${f} uses an HTML-string sink`);
  }
});

test("manifest keeps permissions small and files exist", () => {
  const m = JSON.parse(readFileSync(join(root, "manifest.json"), "utf8"));
  assert.equal(m.manifest_version, 3);
  assert.deepEqual(m.permissions.sort(), ["sidePanel", "storage"]);
  const listed = [m.background.service_worker, m.side_panel.default_path, m.options_page,
    ...m.content_scripts.flatMap((c) => [...c.js, ...c.css]), ...Object.values(m.icons)];
  const present = new Set(readdirSync(root, { recursive: true }));
  for (const f of listed) assert.ok(present.has(f), `missing ${f}`);
});

test("the shared folder is not called lib (ignored by .gitignore)", () => {
  assert.ok(!readdirSync(root).includes("lib"));
});
