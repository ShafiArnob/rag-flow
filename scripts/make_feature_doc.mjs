// Renders a feature doc written in Markdown, with ```mermaid diagrams, to PDF, so every doc in
// docs/feature/ shares one layout. Needs only Node and a Chrome or Edge install:
//   node scripts/make_feature_doc.mjs docs/feature/<feature-name>.md [output.pdf]
// The page loads pinned marked and mermaid from jsDelivr, so the first run needs network access.
// It renders the page once to check every diagram became an SVG, then prints it; a diagram that
// fails to render stops the script instead of reaching the PDF as raw text.

import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const MARKED_URL = "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js";
const MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js";
// Virtual time Chrome gives the page to load the libraries and draw the diagrams.
const RENDER_BUDGET_MILLISECONDS = "30000";
const RENDERED_MARKER = 'data-render-status="ok"';
const BROWSER_CANDIDATES = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
];

const STYLE = `
  @page { size: A4; margin: 18mm 16mm; }
  body { font-family: Arial, Helvetica, sans-serif; font-size: 10.5pt; line-height: 1.45;
         color: #1f2328; background: #ffffff; margin: 0; }
  h1 { font-size: 21pt; margin: 0 0 4pt; }
  h2 { font-size: 13.5pt; margin: 18pt 0 6pt; padding-bottom: 3pt; border-bottom: 1px solid #d0d7de;
       break-after: avoid; }
  h3 { font-size: 11.5pt; margin: 12pt 0 4pt; break-after: avoid; }
  p, ul, ol { margin: 0 0 7pt; }
  li { margin-bottom: 2pt; }
  code { font-family: Arial, Helvetica, sans-serif; background: #f2f4f7; padding: 0 3pt;
         border-radius: 3pt; font-size: 9.5pt; }
  pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 4pt; padding: 7pt 9pt;
        white-space: pre-wrap; break-inside: avoid; }
  pre code { background: none; padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 4pt 0 10pt; font-size: 9pt;
          table-layout: fixed; }
  th, td { border: 1px solid #d0d7de; padding: 4pt 6pt; text-align: left; vertical-align: top;
           overflow-wrap: anywhere; }
  /* The code map: File path | Class / Function | Role. */
  th:nth-child(1):nth-last-child(3) { width: 38%; }
  th:nth-child(2):nth-last-child(2) { width: 30%; }
  th { background: #f2f4f7; }
  tr { break-inside: avoid; }
  .diagram { text-align: center; margin: 8pt 0 12pt; break-inside: avoid; }
  .diagram svg { max-width: 100%; height: auto; }
`;

function findBrowser() {
  const browser = BROWSER_CANDIDATES.find((candidate) => candidate && existsSync(candidate));
  if (!browser) {
    throw new Error(`no Chrome or Edge found; set CHROME_PATH (looked in: ${BROWSER_CANDIDATES.filter(Boolean).join(", ")})`);
  }
  return browser;
}

function buildPage(markdown, title) {
  // JSON plus escaped "<" keeps the source inert inside the script element.
  const source = JSON.stringify(markdown).replace(/</g, "\\u003c");
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${title}</title>
<style>${STYLE}</style>
<script src="${MARKED_URL}"></script>
<script src="${MERMAID_URL}"></script>
</head><body><main id="document"></main>
<script>
(async () => {
  const root = document.getElementById("document");
  root.innerHTML = marked.parse(${source});
  for (const block of root.querySelectorAll("pre > code.language-mermaid")) {
    const diagram = document.createElement("div");
    diagram.className = "diagram mermaid";
    diagram.textContent = block.textContent;
    block.parentElement.replaceWith(diagram);
  }
  mermaid.initialize({ startOnLoad: false, theme: "neutral",
                       themeVariables: { fontFamily: "Arial, Helvetica, sans-serif", fontSize: "15px" },
                       flowchart: { htmlLabels: false, nodeSpacing: 40, rankSpacing: 55 } });
  await mermaid.run({ querySelector: ".mermaid" });
  const diagrams = [...root.querySelectorAll(".mermaid")];
  const allRendered = diagrams.length > 0 && diagrams.every((d) => d.querySelector("svg"));
  document.body.setAttribute("data-render-status", allRendered ? "ok" : "failed");
})().catch((error) => {
  document.body.setAttribute("data-render-status", "failed");
  document.body.setAttribute("data-render-error", String(error));
});
</script></body></html>`;
}

function runBrowser(browser, profileDirectory, extraArguments, pageUrl) {
  return execFileSync(
    browser,
    [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      `--user-data-dir=${profileDirectory}`,
      "--run-all-compositor-stages-before-draw",
      `--virtual-time-budget=${RENDER_BUDGET_MILLISECONDS}`,
      ...extraArguments,
      pageUrl,
    ],
    { encoding: "utf8", maxBuffer: 64 * 1024 * 1024, stdio: ["ignore", "pipe", "ignore"] },
  );
}

function main() {
  const [inputArgument, outputArgument] = process.argv.slice(2);
  if (!inputArgument) {
    throw new Error("usage: node scripts/make_feature_doc.mjs <feature.md> [output.pdf]");
  }
  const input = resolve(inputArgument);
  const output = resolve(outputArgument ?? join(dirname(input), `${basename(input, ".md")}.pdf`));
  const markdown = readFileSync(input, "utf8");
  const title = basename(input, ".md");

  const workDirectory = mkdtempSync(join(tmpdir(), "feature-doc-"));
  try {
    const pagePath = join(workDirectory, `${title}.html`);
    writeFileSync(pagePath, buildPage(markdown, title), "utf8");
    const pageUrl = pathToFileURL(pagePath).href;
    const browser = findBrowser();
    const profileDirectory = join(workDirectory, "profile");

    const renderedDom = runBrowser(browser, profileDirectory, ["--dump-dom"], pageUrl);
    if (!renderedDom.includes(RENDERED_MARKER)) {
      const reason = renderedDom.match(/data-render-error="([^"]*)"/)?.[1] ?? "no diagram rendered";
      throw new Error(`${input}: the diagrams did not render (${reason}); the PDF was not written`);
    }
    runBrowser(browser, profileDirectory, ["--no-pdf-header-footer", `--print-to-pdf=${output}`], pageUrl);
    if (!existsSync(output)) {
      throw new Error(`${browser} exited without writing ${output}`);
    }
    console.log(`wrote ${output}`);
  } finally {
    rmSync(workDirectory, { recursive: true, force: true });
  }
}

main();
