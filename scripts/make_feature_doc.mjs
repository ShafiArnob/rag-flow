// Renders a feature doc written in Markdown to PDF, so every doc in docs/feature/ shares one
// layout. Needs only Node and a Chrome or Edge install:
//   node scripts/make_feature_doc.mjs docs/feature/<feature-name>.md [output.pdf]
// The page loads pinned marked and mermaid from jsDelivr, so the first run needs network access.
// It renders the page once to check every diagram became an SVG, then prints it; a diagram that
// fails to render stops the script instead of reaching the PDF as raw text.
//
// Besides plain Markdown, the source can use these blocks. Cells are separated by " | " and may hold
// inline Markdown.
//   ```mermaid   a diagram: flowchart, sequenceDiagram, stateDiagram-v2, journey, mindmap,
//                timeline or pie, all drawn in PALETTE. Flowcharts get the node classes in
//                NODE_CLASSES (`A:::store`).
//   ```part      one "Label | Title | Who it is for" line: a banner opening a part of the doc.
//                Every part after the first starts on a new page.
//   ```compare   a before/after table: the first line holds the two column titles, each later
//                line one "before | after" pair.
//   ```examples  one "ok | situation | what happens" or "fail | situation | what happens" per
//                line: worked examples, marked as working or failing.
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
  // Extra hues for diagrams that colour by series (mindmap branches, pie slices, journey sections).
  violetFill: "#f1eafb",
  violetStroke: "#7c5cc4",
  roseFill: "#fbe9ef",
  roseStroke: "#c4507c",
  // Examples and before/after: what works, and what fails or is refused.
  goodFill: "#e6f4ea",
  goodStroke: "#2f7d4a",
  badFill: "#fdecec",
  badStroke: "#b42318",
  line: "#4b5563",
};
const SERIES_FILLS = ["coreFill", "storeFill", "observeFill", "violetFill", "roseFill", "externalFill"];
const SERIES_STROKES = ["coreStroke", "storeStroke", "observeStroke", "violetStroke", "roseStroke", "externalStroke"];
// The theme's per-series variables (cScale0, pie1, fillType0, …), drawn from the palette.
const seriesVariables = (prefix, keys, firstIndex) =>
  Object.fromEntries(keys.map((key, index) => [`${prefix}${index + firstIndex}`, PALETTE[key]]));

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
    // Mindmap branches and timeline sections.
    ...seriesVariables("cScale", SERIES_FILLS, 0),
    ...seriesVariables("cScalePeer", SERIES_STROKES, 0),
    ...Object.fromEntries(SERIES_FILLS.map((_, index) => [`cScaleLabel${index}`, PALETTE.ink])),
    // Journey sections.
    ...seriesVariables("fillType", [...SERIES_FILLS, "coreFill", "storeFill"], 0),
    // Pie slices: strong hues, so white labels stay readable on them.
    ...seriesVariables("pie", SERIES_STROKES, 1),
    pieSectionTextColor: "#ffffff",
    pieSectionTextSize: "14px",
    pieStrokeColor: "#ffffff",
    pieOuterStrokeColor: PALETTE.rule,
    pieLegendTextColor: PALETTE.text,
    pieTitleTextColor: PALETTE.ink,
  },
  flowchart: { htmlLabels: true, curve: "basis", nodeSpacing: 32, rankSpacing: 48, padding: 12 },
  sequence: { mirrorActors: false, width: 110, height: 40, actorMargin: 60, boxMargin: 8,
              messageMargin: 30, noteMargin: 8, actorFontSize: 16, messageFontSize: 16,
              noteFontSize: 15 },
  er: { minEntityWidth: 110, entityPadding: 10, fontSize: 14 },
  state: { padding: 10, textHeight: 12 },
  journey: { taskFontSize: 14, taskFontFamily: TEXT_FONT, boxMargin: 8, width: 140 },
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
  /* A list item that introduces a code block stays on the same page as it. */
  li:has(> pre) { break-inside: avoid; }
  /* A printed page has nothing to click: URLs read as text. */
  a { color: inherit; text-decoration: none; }
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
  .part { margin: 18pt 0 4pt; padding: 10pt 14pt; border-radius: 6pt; background: ${PALETTE.coreFill};
          border-left: 5px solid ${PALETTE.coreStroke}; break-after: avoid; }
  .part ~ .part { break-before: page; margin-top: 0; }
  .part-label { font-size: 8pt; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
                color: ${PALETTE.coreStroke}; }
  .part-title { font-size: 17pt; font-weight: 700; color: ${PALETTE.ink}; line-height: 1.25; }
  .part-audience { color: #4b5563; margin-top: 2pt; }
  table.compare { table-layout: fixed; font-size: 9.2pt; break-inside: avoid; }
  table.compare th { width: 50%; border-bottom: none; padding: 6pt 8pt; }
  table.compare th:first-child { background: ${PALETTE.badFill}; color: ${PALETTE.badStroke}; }
  table.compare th:last-child { background: ${PALETTE.goodFill}; color: ${PALETTE.goodStroke}; }
  table.compare td { padding: 6pt 8pt; }
  table.compare td:first-child { border-right: 1px solid ${PALETTE.rule}; }
  .examples { display: grid; grid-template-columns: 1fr 1fr; gap: 8pt; margin: 6pt 0 12pt; }
  .example { border: 1px solid ${PALETTE.rule}; border-left-width: 4px; border-radius: 5pt;
             padding: 7pt 10pt; break-inside: avoid; font-size: 9pt; line-height: 1.45; }
  .example-ok { border-left-color: ${PALETTE.goodStroke}; }
  .example-fail { border-left-color: ${PALETTE.badStroke}; }
  .example-verdict { font-size: 7.5pt; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
  .example-ok .example-verdict { color: ${PALETTE.goodStroke}; }
  .example-fail .example-verdict { color: ${PALETTE.badStroke}; }
  .example-situation { font-weight: 600; color: ${PALETTE.ink}; margin: 2pt 0 3pt; }
  .example-outcome { color: ${PALETTE.text}; }
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

  const cells = (block) => block.textContent.trim().split("\\n").filter((line) => line.trim())
    .map((line) => line.split(" | ").map((cell) => cell.trim()));
  const rows = (block) => cells(block).map((row) => row.map((cell) => marked.parseInline(cell)));
  const element = (className, html, tag = "div") => {
    const node = document.createElement(tag);
    node.className = className;
    node.innerHTML = html;
    return node;
  };
  const examples = { ok: ["example example-ok", "&#10003; Works"], fail: ["example example-fail", "&#10007; Fails"] };

  for (const block of root.querySelectorAll("pre > code.language-part")) {
    const [[label, title, audience]] = rows(block);
    block.parentElement.replaceWith(element("part", '<div class="part-label">' + label + "</div>"
      + '<div class="part-title">' + (title ?? "") + "</div>"
      + (audience ? '<div class="part-audience">' + audience + "</div>" : "")));
  }
  for (const block of root.querySelectorAll("pre > code.language-compare")) {
    const [[before, after], ...pairs] = rows(block);
    const body = pairs.map(([left, right]) => "<tr><td>" + left + "</td><td>" + (right ?? "") + "</td></tr>");
    block.parentElement.replaceWith(element("compare", "<thead><tr><th>&#10007; " + before
      + "</th><th>&#10003; " + after + "</th></tr></thead><tbody>" + body.join("") + "</tbody>", "table"));
  }
  for (const block of root.querySelectorAll("pre > code.language-examples")) {
    const grid = element("examples", "");
    for (const [kind, situation, outcome] of cells(block)) {
      const style = examples[kind];
      if (!style) throw new Error("an examples line must start with ok or fail, not " + JSON.stringify(kind));
      grid.append(element(style[0], '<div class="example-verdict">' + style[1] + "</div>"
        + '<div class="example-situation">' + marked.parseInline(situation ?? "") + "</div>"
        + '<div class="example-outcome">' + marked.parseInline(outcome ?? "") + "</div>"));
    }
    block.parentElement.replaceWith(grid);
  }

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
  for (const container of root.querySelectorAll("td code, .step code, .step-where, .example code")) {
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
