// Renders a feature doc written in Markdown to PDF, so every doc in docs/feature/ shares one
// layout. Needs only Node and a Chrome or Edge install:
//   node scripts/make_feature_doc.mjs docs/feature/<feature-name>.md [output.pdf]
// The page loads pinned marked and mermaid from jsDelivr, so the first run needs network access.
// It renders the page once to check every diagram became an SVG, then prints it; a diagram that
// fails to render stops the script instead of reaching the PDF as raw text.
//
// Besides plain Markdown, the source can use:
//   ```mermaid   a diagram. Flowcharts get the node classes in NODE_CLASSES (`A:::store`).
//   ```steps     one "Title | file or module | what happens" per line: a left-to-right strip.
//   ```cards     one "LABEL | text" per line: a row of summary cards.
//   > quote      a callout box for the one point a reader must not miss.
//   Figure N. …  a paragraph starting this way is styled as the caption of the figure above it.

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

const TEXT_FONT = '"Segoe UI", "Noto Sans", "Helvetica Neue", Arial, sans-serif';
const CODE_FONT = '"Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace';

// The one palette, shared by the page and the diagrams.
const PALETTE = {
  ink: "#111827",
  text: "#1f2937",
  muted: "#6b7280",
  rule: "#d9dde3",
  surface: "#f5f7fa",
  coreFill: "#e8effc",
  coreStroke: "#4a72c4",
  storeFill: "#e6f4ea",
  storeStroke: "#3f8f5a",
  observeFill: "#fdf3dc",
  observeStroke: "#c8962e",
  externalFill: "#f3f4f6",
  externalStroke: "#9ca3af",
  line: "#4b5563",
};

// Node classes every flowchart can use. core: built in this repo (the default look).
// focus: the node the figure is about. store: databases and caches. observe: telemetry.
// external: clients and third parties. planned: designed but not built yet.
const NODE_CLASSES = {
  core: `fill:${PALETTE.coreFill},stroke:${PALETTE.coreStroke},stroke-width:1.2px,color:${PALETTE.ink}`,
  focus: `fill:${PALETTE.coreFill},stroke:${PALETTE.coreStroke},stroke-width:2.6px,color:${PALETTE.ink}`,
  store: `fill:${PALETTE.storeFill},stroke:${PALETTE.storeStroke},stroke-width:1.2px,color:${PALETTE.ink}`,
  observe: `fill:${PALETTE.observeFill},stroke:${PALETTE.observeStroke},stroke-width:1.2px,color:${PALETTE.ink}`,
  external: `fill:${PALETTE.externalFill},stroke:${PALETTE.externalStroke},stroke-width:1.2px,color:${PALETTE.ink}`,
  planned: `fill:#ffffff,stroke:${PALETTE.externalStroke},stroke-width:1.2px,stroke-dasharray:5 4,color:${PALETTE.muted}`,
};

const MERMAID_CONFIG = {
  startOnLoad: false,
  theme: "base",
  themeVariables: {
    fontFamily: TEXT_FONT,
    fontSize: "14px",
    primaryColor: PALETTE.coreFill,
    primaryBorderColor: PALETTE.coreStroke,
    primaryTextColor: PALETTE.ink,
    secondaryColor: PALETTE.storeFill,
    tertiaryColor: PALETTE.surface,
    lineColor: PALETTE.line,
    textColor: PALETTE.text,
    edgeLabelBackground: "#ffffff",
    clusterBkg: PALETTE.surface,
    clusterBorder: PALETTE.rule,
    actorBkg: PALETTE.coreFill,
    actorBorder: PALETTE.coreStroke,
    actorTextColor: PALETTE.ink,
    actorLineColor: PALETTE.rule,
    signalColor: PALETTE.line,
    signalTextColor: PALETTE.ink,
    labelBoxBkgColor: PALETTE.surface,
    labelBoxBorderColor: PALETTE.rule,
    noteBkgColor: PALETTE.observeFill,
    noteBorderColor: PALETTE.observeStroke,
    noteTextColor: PALETTE.ink,
    activationBkgColor: PALETTE.coreFill,
    activationBorderColor: PALETTE.coreStroke,
    attributeBackgroundColorOdd: "#ffffff",
    attributeBackgroundColorEven: PALETTE.surface,
  },
  flowchart: { htmlLabels: true, curve: "basis", nodeSpacing: 32, rankSpacing: 48, padding: 12 },
  sequence: { mirrorActors: false, width: 110, height: 40, actorMargin: 60, boxMargin: 8,
              messageMargin: 30, noteMargin: 8, actorFontSize: 16, messageFontSize: 16,
              noteFontSize: 15 },
  er: { minEntityWidth: 110, entityPadding: 10, fontSize: 14 },
};

const STYLE = `
  @page { size: A4; margin: 16mm 15mm; }
  body { font-family: ${TEXT_FONT}; font-size: 10pt; line-height: 1.5; color: ${PALETTE.text};
         background: #ffffff; margin: 0; }
  h1 { font-size: 22pt; line-height: 1.2; color: ${PALETTE.ink}; margin: 0 0 6pt; }
  h1 + p { color: #4b5563; font-size: 10.5pt; margin-bottom: 12pt; }
  h2 { font-size: 14pt; color: ${PALETTE.ink}; margin: 20pt 0 8pt; padding-bottom: 4pt;
       border-bottom: 1.5px solid ${PALETTE.ink}; break-after: avoid; }
  h3 { font-size: 11pt; color: ${PALETTE.ink}; margin: 14pt 0 5pt; break-after: avoid; }
  p, ul, ol { margin: 0 0 7pt; }
  li { margin-bottom: 3pt; }
  strong { color: ${PALETTE.ink}; }
  code { font-family: ${CODE_FONT}; font-size: 8.6pt; background: #f1f3f6; padding: 0.5pt 3pt;
         border-radius: 3pt; }
  pre { background: ${PALETTE.surface}; border: 1px solid ${PALETTE.rule}; border-radius: 5pt;
        padding: 7pt 10pt; white-space: pre-wrap; break-inside: avoid; }
  pre code { background: none; padding: 0; font-size: 8.4pt; }
  blockquote { margin: 8pt 0 10pt; padding: 7pt 11pt; background: ${PALETTE.surface};
               border-left: 3px solid ${PALETTE.coreStroke}; border-radius: 0 4pt 4pt 0;
               break-inside: avoid; }
  blockquote p:last-child { margin-bottom: 0; }
  table { border-collapse: collapse; width: 100%; margin: 4pt 0 12pt; font-size: 8.8pt; }
  th, td { padding: 4.5pt 6pt; text-align: left; vertical-align: top; }
  th { font-weight: 600; color: ${PALETTE.ink}; border-bottom: 1.5px solid ${PALETTE.ink}; }
  td { border-bottom: 1px solid ${PALETTE.rule}; }
  td code { background: none; padding: 0; }
  /* The code map: File path | Class / Function | Role. */
  th:nth-child(1):nth-last-child(3) { width: 36%; }
  th:nth-child(2):nth-last-child(2) { width: 30%; }
  tr { break-inside: avoid; }
  .diagram { text-align: center; margin: 10pt 0 4pt; break-inside: avoid; }
  .diagram svg { max-width: 100%; height: auto; }
  .caption { font-size: 8.5pt; color: ${PALETTE.muted}; margin: 2pt 0 12pt; break-before: avoid; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(0, 1fr)); gap: 8pt;
           margin: 4pt 0 12pt; break-inside: avoid; }
  .card { border: 1px solid ${PALETTE.rule}; border-radius: 5pt; padding: 8pt 10pt; }
  .card-label { font-size: 7.5pt; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
                color: #4b5563; margin-bottom: 3pt; }
  .steps { display: flex; align-items: stretch; gap: 4pt; margin: 8pt 0 4pt; break-inside: avoid; }
  .step { flex: 1 1 0; min-width: 0; border: 1px solid ${PALETTE.coreStroke}; background: ${PALETTE.coreFill};
          border-radius: 5pt; padding: 7pt 8pt; font-size: 8.6pt; line-height: 1.4; }
  .step-title { font-weight: 700; color: ${PALETTE.ink}; font-size: 9pt; }
  .step-where { font-family: ${CODE_FONT}; font-size: 7.6pt; color: ${PALETTE.muted}; margin: 1pt 0 4pt; }
  .step code { background: rgba(255, 255, 255, 0.7); }
  .step-arrow { align-self: center; color: ${PALETTE.muted}; font-size: 11pt; }
`;

function findBrowser() {
  const browser = BROWSER_CANDIDATES.find((candidate) => candidate && existsSync(candidate));
  if (!browser) {
    throw new Error(`no Chrome or Edge found; set CHROME_PATH (looked in: ${BROWSER_CANDIDATES.filter(Boolean).join(", ")})`);
  }
  return browser;
}

// JSON plus escaped "<" keeps a value inert inside the page's script element.
function toScriptLiteral(value) {
  return JSON.stringify(value).replace(/</g, "\\u003c");
}

function buildPage(markdown, title) {
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${title}</title>
<style>${STYLE}</style>
<script src="${MARKED_URL}"></script>
<script src="${MERMAID_URL}"></script>
</head><body><main id="document"></main>
<script>
(async () => {
  const nodeClasses = ${toScriptLiteral(NODE_CLASSES)};
  const classDefinitions = Object.entries(nodeClasses)
    .map(([name, style]) => "classDef " + name + " " + style).join("\\n");
  const root = document.getElementById("document");
  root.innerHTML = marked.parse(${toScriptLiteral(markdown)});

  const rows = (block) => block.textContent.trim().split("\\n").filter((line) => line.trim())
    .map((line) => line.split(" | ").map((cell) => marked.parseInline(cell.trim())));
  const element = (className, html) => {
    const node = document.createElement("div");
    node.className = className;
    node.innerHTML = html;
    return node;
  };

  for (const block of root.querySelectorAll("pre > code.language-mermaid")) {
    const source = block.textContent.trim();
    const isFlowchart = /^(flowchart|graph)\\b/.test(source);
    const diagram = element("diagram mermaid", "");
    diagram.textContent = isFlowchart ? source + "\\n" + classDefinitions : source;
    block.parentElement.replaceWith(diagram);
  }
  for (const block of root.querySelectorAll("pre > code.language-steps")) {
    const strip = element("steps", "");
    rows(block).forEach(([title, where, what], index) => {
      if (index > 0) strip.append(element("step-arrow", "&rarr;"));
      strip.append(element("step", '<div class="step-title">' + title + "</div>"
        + '<div class="step-where">' + (where ?? "") + "</div><div>" + (what ?? "") + "</div>"));
    });
    block.parentElement.replaceWith(strip);
  }
  for (const block of root.querySelectorAll("pre > code.language-cards")) {
    const row = element("cards", "");
    for (const [label, text] of rows(block)) {
      row.append(element("card", '<div class="card-label">' + label + "</div><div>" + (text ?? "") + "</div>"));
    }
    block.parentElement.replaceWith(row);
  }
  // Long paths and identifiers in narrow cells wrap after a "/" or "_", never mid-word.
  for (const container of root.querySelectorAll("td code, .step code, .step-where")) {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) walker.currentNode.data = walker.currentNode.data.replace(/([\\/_])/g, "$1\\u200b");
  }
  for (const paragraph of root.querySelectorAll("p")) {
    if (/^Figure \\d+\\./.test(paragraph.textContent)) paragraph.classList.add("caption");
  }

  mermaid.initialize(${toScriptLiteral(MERMAID_CONFIG)});
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
