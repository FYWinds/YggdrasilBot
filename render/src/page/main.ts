// Browser side of the render service: turns Markdown / Mermaid / HTML into laid-out DOM and reports the
// regions ("shots") the server should screenshot. Everything here runs in a page whose network is limited to
// the bundled assets.
import { alert } from "@mdit/plugin-alert";
import { footnote } from "@mdit/plugin-footnote";
import { katex } from "@mdit/plugin-katex-slim";
import { tasklist } from "@mdit/plugin-tasklist";
import DOMPurify from "dompurify";
import type MermaidModule from "mermaid";
import MarkdownIt from "markdown-it";
import { PeleError, render as peleRender } from "pele";
import { createHighlighterCoreSync } from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";
import bash from "@shikijs/langs/bash";
import c from "@shikijs/langs/c";
import cpp from "@shikijs/langs/cpp";
import csharp from "@shikijs/langs/csharp";
import css from "@shikijs/langs/css";
import diff from "@shikijs/langs/diff";
import docker from "@shikijs/langs/docker";
import go from "@shikijs/langs/go";
import html from "@shikijs/langs/html";
import ini from "@shikijs/langs/ini";
import java from "@shikijs/langs/java";
import javascript from "@shikijs/langs/javascript";
import json from "@shikijs/langs/json";
import jsx from "@shikijs/langs/jsx";
import kotlin from "@shikijs/langs/kotlin";
import markdown from "@shikijs/langs/markdown";
import python from "@shikijs/langs/python";
import rust from "@shikijs/langs/rust";
import sql from "@shikijs/langs/sql";
import toml from "@shikijs/langs/toml";
import tsx from "@shikijs/langs/tsx";
import typescript from "@shikijs/langs/typescript";
import xml from "@shikijs/langs/xml";
import yaml from "@shikijs/langs/yaml";
import githubLight from "@shikijs/themes/github-light";

export type Kind = "markdown" | "mermaid" | "html";

/** A region to screenshot, in CSS px relative to the document. */
export interface Shot {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type RenderOutcome =
  | { ok: true; shots: Shot[]; warnings: string[] }
  | { ok: false; error: string };

const SANS = "'Noto Sans CJK SC', 'Noto Color Emoji', sans-serif";
const FONT_SIZE = 16;

// Markdown layout (CSS px).
const BODY_WIDTH = 760;
const BODY_MAX_WIDTH = 1100;
const PAGE_PAD = 24;
const PAGE_HEIGHT = 2000;
// A single block up to this tall keeps its own page; taller blocks are cut into PAGE_HEIGHT slices.
const BLOCK_MAX_HEIGHT = 3000;

// Diagram scaling: aim for ~1600 device px wide at DSF 2, never upscale more than 2x, cap device pixels.
const DIAGRAM_TARGET_WIDTH = 800;
const DIAGRAM_PAD = 16;
const DSF = 2;
const MAX_DEVICE_PIXELS = 16_000_000;

// HTML layout.
const HTML_MAX_HEIGHT = 4000;

const highlighter = createHighlighterCoreSync({
  themes: [githubLight],
  langs: [
    bash, c, cpp, csharp, css, diff, docker, go, html, ini, java, javascript, json, jsx, kotlin, markdown, python,
    rust, sql, toml, tsx, typescript, xml, yaml,
  ],
  engine: createJavaScriptRegexEngine(),
});
const knownLangs = new Set(highlighter.getLoadedLanguages());

const ALERT_ICONS: Record<string, [string, string]> = {
  note: [
    "注意",
    "M0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8Zm8-6.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM6.5 7.75A.75.75 0 0 1 7.25 7h1a.75.75 0 0 1 .75.75v2.75h.25a.75.75 0 0 1 0 1.5h-2a.75.75 0 0 1 0-1.5h.25v-2h-.25a.75.75 0 0 1-.75-.75ZM8 6a1 1 0 1 1 0-2 1 1 0 0 1 0 2Z",
  ],
  tip: [
    "提示",
    "M8 1.5c-2.363 0-4 1.69-4 3.75 0 .984.424 1.625.984 2.304l.214.253c.223.264.47.556.673.848.284.411.537.896.621 1.49a.75.75 0 0 1-1.484.211c-.04-.282-.163-.547-.37-.847a8.456 8.456 0 0 0-.542-.68c-.084-.1-.173-.205-.268-.32C3.201 7.75 2.5 6.766 2.5 5.25 2.5 2.31 4.863 0 8 0s5.5 2.31 5.5 5.25c0 1.516-.701 2.5-1.328 3.259-.095.115-.184.22-.268.319-.207.245-.383.453-.541.681-.208.3-.33.565-.37.847a.751.751 0 0 1-1.485-.212c.084-.593.337-1.078.621-1.489.203-.292.45-.584.673-.848.075-.088.147-.173.213-.253.561-.679.985-1.32.985-2.304 0-2.06-1.637-3.75-4-3.75ZM5.75 12h4.5a.75.75 0 0 1 0 1.5h-4.5a.75.75 0 0 1 0-1.5ZM6 15.25a.75.75 0 0 1 .75-.75h2.5a.75.75 0 0 1 0 1.5h-2.5a.75.75 0 0 1-.75-.75Z",
  ],
  important: [
    "重要",
    "M0 1.75C0 .784.784 0 1.75 0h12.5C15.216 0 16 .784 16 1.75v9.5A1.75 1.75 0 0 1 14.25 13H8.06l-2.573 2.573A1.458 1.458 0 0 1 3 14.543V13H1.75A1.75 1.75 0 0 1 0 11.25Zm1.75-.25a.25.25 0 0 0-.25.25v9.5c0 .138.112.25.25.25h2a.75.75 0 0 1 .75.75v2.19l2.72-2.72a.749.749 0 0 1 .53-.22h6.5a.25.25 0 0 0 .25-.25v-9.5a.25.25 0 0 0-.25-.25Zm7 2.25v2.5a.75.75 0 0 1-1.5 0v-2.5a.75.75 0 0 1 1.5 0ZM9 9a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z",
  ],
  warning: [
    "警告",
    "M6.457 1.047c.659-1.234 2.427-1.234 3.086 0l6.082 11.378A1.75 1.75 0 0 1 14.082 15H1.918a1.75 1.75 0 0 1-1.543-2.575Zm1.763.707a.25.25 0 0 0-.44 0L1.698 13.132a.25.25 0 0 0 .22.368h12.164a.25.25 0 0 0 .22-.368Zm.53 3.996v2.5a.75.75 0 0 1-1.5 0v-2.5a.75.75 0 0 1 1.5 0ZM9 11a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z",
  ],
  caution: [
    "危险",
    "M4.47.22A.749.749 0 0 1 5 0h6c.199 0 .389.079.53.22l4.25 4.25c.141.14.22.331.22.53v6a.749.749 0 0 1-.22.53l-4.25 4.25A.749.749 0 0 1 11 16H5a.749.749 0 0 1-.53-.22L.22 11.53A.749.749 0 0 1 0 11V5c0-.199.079-.389.22-.53Zm.84 1.28L1.5 5.31v5.38l3.81 3.81h5.38l3.81-3.81V5.31L10.69 1.5ZM8 4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 8 4Zm0 8a1 1 0 1 1 0-2 1 1 0 0 1 0 2Z",
  ],
};

// Per-render side channel filled by the renderer rules below (md.render is synchronous; one render at a time).
const mdState = { mermaid: [] as string[], externalImages: 0 };

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight(code, lang) {
    const name = lang.trim().toLowerCase();
    if (!name || !knownLangs.has(name)) return "";
    return highlighter.codeToHtml(code.replace(/\n$/, ""), { lang: name, theme: "github-light" });
  },
})
  .use(katex, { throwOnError: false, output: "html", logger: () => "ignore" as const })
  .use(tasklist, { label: false })
  .use(footnote)
  .use(alert, {
    titleRenderer(tokens, idx) {
      const name = tokens[idx].markup;
      const [title, path] = ALERT_ICONS[name] ?? ALERT_ICONS.note;
      return (
        `<p class="markdown-alert-title"><svg class="octicon" viewBox="0 0 16 16" width="16" height="16" ` +
        `aria-hidden="true"><path d="${path}"></path></svg>${title}</p>\n`
      );
    },
  });

const defaultFence = md.renderer.rules.fence!;
md.renderer.rules.fence = (tokens, idx, options, env, self) => {
  const token = tokens[idx];
  if (token.info.trim().split(/\s+/)[0].toLowerCase() === "mermaid") {
    mdState.mermaid.push(token.content);
    return `<div class="mermaid-block" data-index="${mdState.mermaid.length - 1}"></div>\n`;
  }
  return defaultFence(tokens, idx, options, env, self);
};

md.renderer.rules.image = (tokens, idx) => {
  const token = tokens[idx];
  const src = String(token.attrGet("src") ?? "");
  const alt = md.utils.escapeHtml(token.content);
  if (/^data:image\/(png|jpe?g|gif|webp);/i.test(src)) {
    return `<img src="${md.utils.escapeHtml(src)}" alt="${alt}">`;
  }
  mdState.externalImages += 1;
  return `<span class="image-placeholder">[图片${alt ? `：${alt}` : ""}]</span>`;
};

// --- Mermaid ---

const PELE_BASE = {
  fontFamily: SANS,
  fontSize: FONT_SIZE,
  responsive: false,
  autoDirection: false,
  links: false,
  images: false,
} as const;

interface Diagram {
  svg: SVGSVGElement;
  width: number;
  height: number;
}

function parseSvg(markup: string): SVGSVGElement {
  const tpl = document.createElement("template");
  tpl.innerHTML = markup;
  const svg = tpl.content.querySelector("svg");
  if (!svg) throw new Error("renderer returned no SVG");
  return svg;
}

let mermaidModule: Promise<{ default: typeof MermaidModule }> | null = null;
let mermaidSeq = 0;

async function renderWithMermaid(source: string): Promise<Diagram> {
  if (!mermaidModule) {
    mermaidModule = import("mermaid").then((m) => {
      m.default.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "base",
        fontFamily: SANS,
        htmlLabels: false,
        themeVariables: {
          fontFamily: SANS,
          fontSize: `${FONT_SIZE}px`,
          background: "#ffffff",
          primaryColor: "#f6f8fa",
          primaryBorderColor: "#d1d9e0",
          primaryTextColor: "#1f2328",
          secondaryColor: "#eef2f6",
          secondaryBorderColor: "#d1d9e0",
          tertiaryColor: "#ffffff",
          tertiaryBorderColor: "#d1d9e0",
          lineColor: "#59636e",
          textColor: "#1f2328",
          noteBkgColor: "#fff8c5",
          noteBorderColor: "#d4a72c",
        },
      });
      return m;
    });
  }
  const mermaid = (await mermaidModule).default;
  const id = `ygg-mermaid-${++mermaidSeq}`;
  try {
    const { svg } = await mermaid.render(id, source);
    const el = parseSvg(svg);
    const box = el.viewBox.baseVal;
    const width = box && box.width ? box.width : Number.parseFloat(el.getAttribute("width") ?? "0");
    const height = box && box.height ? box.height : Number.parseFloat(el.getAttribute("height") ?? "0");
    el.removeAttribute("style");
    return { svg: el, width, height };
  } finally {
    // mermaid leaves its scratch / error containers in <body> on failure.
    document.getElementById(id)?.remove();
    document.getElementById(`d${id}`)?.remove();
  }
}

function peleMessage(err: PeleError): string {
  const where = err.line > 0 ? `（第 ${err.line} 行${err.column > 0 ? `第 ${err.column} 列` : ""}）` : "";
  // Pele's parse messages usually already quote the source line with a caret; add the snippet only if not.
  const snippet = err.snippet && !err.message.includes(err.snippet.trim()) ? `\n${err.snippet}` : "";
  return `Mermaid 语法错误${where}：\n${err.message}${snippet}`;
}

/** Pele first; mermaid@12 only when Pele rejects the source. Throws a model-facing message if both fail. */
async function renderDiagram(source: string, maxWidth: number): Promise<Diagram> {
  try {
    const r = peleRender(source, { ...PELE_BASE, maxWidth });
    return { svg: parseSvg(r.svg), width: r.width, height: r.height };
  } catch (err) {
    if (!(err instanceof PeleError)) throw err;
    try {
      return await renderWithMermaid(source);
    } catch {
      throw new DiagramError(peleMessage(err));
    }
  }
}

class DiagramError extends Error {}

function sizeSvg(d: Diagram, scale: number): void {
  d.svg.setAttribute("width", String(Math.round(d.width * scale)));
  d.svg.setAttribute("height", String(Math.round(d.height * scale)));
  if (!d.svg.getAttribute("viewBox")) d.svg.setAttribute("viewBox", `0 0 ${d.width} ${d.height}`);
  d.svg.style.display = "block";
}

// --- Layout helpers ---

const out = document.getElementById("out")!;

function shotOf(el: Element): Shot {
  const r = el.getBoundingClientRect();
  return {
    x: Math.floor(r.left + window.scrollX),
    y: Math.floor(r.top + window.scrollY),
    width: Math.ceil(r.width),
    height: Math.ceil(r.height),
  };
}

async function settle(): Promise<void> {
  await document.fonts.ready;
  const frame = Promise.withResolvers<void>();
  requestAnimationFrame(() => frame.resolve());
  await frame.promise;
}

// --- Kinds ---

async function renderMermaid(source: string): Promise<RenderOutcome> {
  const d = await renderDiagram(source, 1200);
  let scale = Math.min(2, Math.max(1, DIAGRAM_TARGET_WIDTH / d.width));
  const cap = Math.sqrt(MAX_DEVICE_PIXELS / (DSF * DSF * d.width * d.height));
  scale = Math.min(scale, cap);
  sizeSvg(d, scale);
  const box = document.createElement("div");
  box.className = "diagram";
  box.style.padding = `${DIAGRAM_PAD}px`;
  box.append(d.svg);
  out.append(box);
  await settle();
  return { ok: true, shots: [shotOf(box)], warnings: [] };
}

async function renderMarkdown(source: string, maxImages: number): Promise<RenderOutcome> {
  mdState.mermaid = [];
  mdState.externalImages = 0;
  const warnings: string[] = [];
  const article = document.createElement("article");
  article.className = "markdown-body";
  article.style.width = `${BODY_WIDTH}px`;
  article.innerHTML = md.render(source);
  out.append(article);
  const { mermaid: diagrams, externalImages } = mdState;

  const blocks = article.querySelectorAll<HTMLElement>(".mermaid-block");
  for (const block of blocks) {
    const src = diagrams[Number(block.dataset.index)];
    try {
      const d = await renderDiagram(src, BODY_WIDTH);
      sizeSvg(d, Math.min(1, BODY_WIDTH / d.width));
      block.append(d.svg);
    } catch (err) {
      const msg = err instanceof DiagramError ? err.message : String(err);
      warnings.push(`第 ${Number(block.dataset.index) + 1} 个 mermaid 图渲染失败，已按代码显示。${msg}`);
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = src;
      pre.append(code);
      block.replaceWith(pre);
    }
  }
  if (externalImages) warnings.push(`外部图片不会加载，${externalImages} 张图片只显示了替代文字。`);

  await settle();

  // Widen the body (up to BODY_MAX_WIDTH) when a table or display formula overflows.
  let overflow = 0;
  for (const el of article.querySelectorAll<HTMLElement>("table, .katex-block")) {
    overflow = Math.max(overflow, el.scrollWidth - el.clientWidth);
  }
  const width = Math.min(BODY_MAX_WIDTH, BODY_WIDTH + Math.ceil(overflow));
  if (width !== BODY_WIDTH) {
    article.style.width = `${width}px`;
    await settle();
  }

  // Greedy pagination over top-level blocks. Heights include the gap to the previous block.
  const children = Array.from(article.children) as HTMLElement[];
  const pages: HTMLElement[][] = [];
  let current: HTMLElement[] = [];
  let top = 0;
  const flush = () => {
    if (current.length) pages.push(current);
    current = [];
  };
  for (const child of children) {
    const rect = child.getBoundingClientRect();
    if (!current.length) top = rect.top;
    if (current.length && rect.bottom - top + 2 * PAGE_PAD > PAGE_HEIGHT) {
      // Keep a trailing heading with the block it introduces.
      const carry: HTMLElement[] = [];
      while (current.length > 1 && /^H[1-6]$/.test(current[current.length - 1].tagName)) carry.unshift(current.pop()!);
      flush();
      current = carry;
      top = (carry[0] ?? child).getBoundingClientRect().top;
    }
    current.push(child);
  }
  flush();

  // Move each page's blocks into its own article so first/last margins collapse like a fresh document.
  const shotsEls: HTMLElement[] = [];
  const pageEls = pages.map((blocks) => {
    const page = document.createElement("div");
    page.className = "page";
    page.style.padding = `${PAGE_PAD}px`;
    const body = document.createElement("article");
    body.className = "markdown-body";
    body.style.width = `${width}px`;
    body.append(...blocks);
    page.append(body);
    return page;
  });
  article.replaceWith(...pageEls);
  await settle();

  for (const page of pageEls) {
    const height = page.getBoundingClientRect().height;
    if (height <= BLOCK_MAX_HEIGHT) {
      shotsEls.push(page);
      continue;
    }
    // Unavoidable mid-block split: show the page through PAGE_HEIGHT windows.
    const slices = Math.ceil(height / PAGE_HEIGHT);
    const sliceHeight = Math.ceil(height / slices);
    for (let i = 0; i < slices; i++) {
      const win = document.createElement("div");
      win.className = "slice";
      win.style.height = `${Math.min(sliceHeight, height - i * sliceHeight)}px`;
      const inner = page.cloneNode(true) as HTMLElement;
      inner.style.marginTop = `${-i * sliceHeight}px`;
      win.append(inner);
      page.before(win);
      shotsEls.push(win);
    }
    page.remove();
  }

  if (shotsEls.length > maxImages) {
    return {
      ok: false,
      error:
        `这段 Markdown 渲染后需要 ${shotsEls.length} 张图，超过了本次最多 ${maxImages} 张的限制。` +
        `请删减内容（大约删到原来的 ${Math.floor((maxImages / shotsEls.length) * 100)}%）后重试。`,
    };
  }
  await settle();
  return { ok: true, shots: shotsEls.map(shotOf), warnings };
}

// --- HTML ---

const FORBID_TAGS = [
  "script", "style", "link", "meta", "base", "iframe", "frame", "frameset", "object", "embed", "applet", "form",
  "input", "button", "select", "option", "textarea", "audio", "video", "source", "track", "template", "dialog",
  "portal", "noscript",
];
const URL_ATTRS = ["src", "href", "xlink:href", "action", "formaction", "poster", "background", "srcset"];

// A CSS value that could fetch: url()/image-set() other than data: URIs and same-document #fragment references.
const FETCHING_CSS = /image-set\(|url\(\s*(?!["']?\s*(?:data:|#))/i;

/** Resolves CSS escapes (`\75 rl(` → `url(`) so they cannot hide a url() from FETCHING_CSS. */
function unescapeCss(value: string): string {
  return value
    .replace(/\\([0-9a-f]{1,6})\s?/gi, (_, hex: string) => String.fromCodePoint(Number.parseInt(hex, 16) || 0xfffd))
    .replace(/\\(.)/g, "$1");
}

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (!(node instanceof Element)) return;
  for (const attr of URL_ATTRS) {
    const value = node.getAttribute(attr);
    if (value === null) continue;
    const keep = attr !== "srcset" && (/^data:image\//i.test(value.trim()) || (attr === "href" && value.startsWith("#")));
    if (!keep) node.removeAttribute(attr);
  }
  // SVG presentation attributes (fill, filter, clip-path, mask, marker-*, cursor, ...) take CSS url() values.
  for (const { name, value } of Array.from(node.attributes)) {
    if (name !== "style" && FETCHING_CSS.test(unescapeCss(value))) node.removeAttribute(name);
  }
  const style = (node as HTMLElement).style as CSSStyleDeclaration | undefined;
  if (!style || !node.hasAttribute("style")) return;
  // Inspect the parsed declarations (escapes resolved, custom properties included) and drop any that could fetch.
  for (const prop of Array.from(style)) {
    if (FETCHING_CSS.test(style.getPropertyValue(prop))) style.removeProperty(prop);
  }
  if (!style.length) node.removeAttribute("style");
});

async function renderHtml(source: string): Promise<RenderOutcome> {
  const clean = DOMPurify.sanitize(source, {
    FORBID_TAGS,
    FORBID_ATTR: ["srcset", "ping", "formaction", "action"],
    ALLOW_DATA_ATTR: false,
    ALLOW_UNKNOWN_PROTOCOLS: false,
  });
  const box = document.createElement("div");
  box.className = "html-body";
  box.style.width = `${BODY_WIDTH}px`;
  box.innerHTML = clean;
  // External images were stripped of their src; show their alt text instead of a broken-image icon.
  for (const img of box.querySelectorAll("img:not([src])")) {
    const alt = img.getAttribute("alt");
    const span = document.createElement("span");
    span.className = "image-placeholder";
    span.textContent = `[图片${alt ? `：${alt}` : ""}]`;
    img.replaceWith(span);
  }
  const page = document.createElement("div");
  page.className = "page";
  page.style.padding = `${PAGE_PAD}px`;
  page.append(box);
  out.append(page);
  await settle();
  if (box.scrollWidth > box.clientWidth) {
    box.style.width = `${Math.min(BODY_MAX_WIDTH, box.scrollWidth)}px`;
    await settle();
  }
  const warnings: string[] = [];
  if (!box.textContent?.trim() && !box.querySelector("img, svg, canvas, table, hr")) {
    return { ok: false, error: "HTML 净化后没有可显示的内容（脚本、表单、外部资源都会被移除）。" };
  }
  const shot = shotOf(page);
  if (shot.height > HTML_MAX_HEIGHT) {
    shot.height = HTML_MAX_HEIGHT;
    warnings.push(`内容超过 ${HTML_MAX_HEIGHT} 像素高，已截断。`);
  }
  return { ok: true, shots: [shot], warnings };
}

// --- Entry ---

function reset(): void {
  out.replaceChildren();
  window.scrollTo(0, 0);
}

async function render(kind: Kind, source: string, maxImages: number): Promise<RenderOutcome> {
  reset();
  try {
    if (kind === "mermaid") return await renderMermaid(source);
    if (kind === "markdown") return await renderMarkdown(source, maxImages);
    return await renderHtml(source);
  } catch (err) {
    if (err instanceof DiagramError) return { ok: false, error: err.message };
    throw err;
  }
}

declare global {
  interface Window {
    ygg: { ready: boolean; render: typeof render; reset: typeof reset };
  }
}

window.ygg = { ready: true, render, reset };
