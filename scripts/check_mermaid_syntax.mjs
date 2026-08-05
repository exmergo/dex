/**
 * Parse every .mmd in a directory with the real Mermaid parser.
 *
 * Usage:
 *   node scripts/check_mermaid_syntax.mjs <fixture-dir>
 *
 * dex emits Mermaid and never renders it, so no Python dependency pins the
 * syntax dialect `explore diagram` has to stay compatible with. The contract is
 * with a parser living in whatever tool a reader opens the diagram in, and the
 * repo rule is that a contract with someone outside this repository gets a test
 * that runs outside this repository. This is that test: it runs the actual
 * mermaid package rather than a Python port of it, which is why it lives in CI
 * behind npm instead of in `pyproject.toml`.
 *
 * Advisory, like the sqlglot canary, and for the same reason: it tracks a moving
 * upstream, so it should tell us a Mermaid major broke our dialect without
 * blocking a merge that did nothing wrong.
 *
 * Mermaid expects a browser, so jsdom supplies one. `mermaid.parse` throws on a
 * syntax error and resolves with the detected diagram type otherwise; both are
 * checked, since a diagram that parses as something other than `er` means the
 * renderer emitted a header Mermaid did not recognize.
 */

import fs from "node:fs";
import path from "node:path";
import { JSDOM } from "jsdom";

const dir = process.argv[2];
if (!dir) {
  console.error("usage: node scripts/check_mermaid_syntax.mjs <fixture-dir>");
  process.exit(2);
}

const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
// `navigator` is getter-only on globalThis in modern Node, so it cannot be
// assigned the way the other two can.
Object.defineProperty(globalThis, "navigator", {
  value: dom.window.navigator,
  configurable: true,
});

const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false });

const files = fs
  .readdirSync(dir)
  .filter((f) => f.endsWith(".mmd"))
  .sort();

if (files.length === 0) {
  console.error(`no .mmd fixtures in ${dir}: nothing was checked`);
  process.exit(2);
}

let failed = 0;
for (const file of files) {
  const full = path.join(dir, file);
  const text = fs.readFileSync(full, "utf8");
  try {
    const result = await mermaid.parse(text);
    const type = result?.diagramType;
    if (type !== "er") {
      console.log(`FAIL ${file}: parsed as '${type}', expected 'er'`);
      failed += 1;
      continue;
    }
    console.log(`ok   ${file}`);
  } catch (err) {
    console.log(`FAIL ${file}`);
    console.log(String(err?.message ?? err).split("\n").slice(0, 8).join("\n"));
    console.log("--- the fixture that failed ---");
    console.log(text);
    failed += 1;
  }
}

console.log(`\n${files.length - failed}/${files.length} fixtures parsed`);
if (failed > 0) {
  console.log(
    "The rendered dialect no longer parses. Either Mermaid changed under us " +
      "(check their changelog for the pinned major) or explore/diagram.py " +
      "emitted something outside the conservative erDiagram subset it commits to.",
  );
  process.exit(1);
}
