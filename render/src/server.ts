// Render service: an HTTP API on :8080 (POST /render) backed by a pool of pre-warmed pages in one Chromium that load
// the bundled render page. (read_url's browser tier is the separate browser service.)
import { readdir, readFile } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import path from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { type Browser, type BrowserContext, chromium, type Page, type Route } from "playwright";
import type { Kind, RenderOutcome } from "./page/main.js";

const HTTP_PORT = Number(process.env.RENDER_HTTP_PORT ?? 8080);
const POOL_SIZE = Math.max(1, Number(process.env.RENDER_POOL_SIZE ?? 2));
const QUEUE_TIMEOUT_MS = Number(process.env.RENDER_QUEUE_TIMEOUT_MS ?? 10_000);
const RENDER_TIMEOUT_MS = Number(process.env.RENDER_TIMEOUT_MS ?? 15_000);
const LOG_REQUESTS = process.env.RENDER_LOG_REQUESTS === "1";

const MAX_SOURCE_CHARS = 20_000;
const MAX_BODY_BYTES = 1 << 20;
const MAX_IMAGES = 4;
const KINDS: Record<Kind, true> = { markdown: true, mermaid: true, html: true };

// The render page is served from this (unresolvable) origin through a route that fulfills from disk.
const ORIGIN = "http://ygg-render.invalid/";
const PAGE_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "page");
const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".ttf": "font/ttf",
};

const WARMUP_MARKDOWN = [
  "# 预热 **Warm-up**",
  "",
  "行内公式 $E = mc^2$，代码 `x`。",
  "",
  "$$\\sum_{i=1}^n i = \\frac{n(n+1)}{2}$$",
  "",
  "```python\nprint('hi')\n```",
  "",
  "```mermaid\nflowchart LR\n  A[开始] --> B[结束]\n```",
].join("\n");

function log(message: string, fields: Record<string, unknown> = {}): void {
  const extra = Object.entries(fields)
    .map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : v}`)
    .join(" ");
  console.log(`${new Date().toISOString()} ${message}${extra ? ` ${extra}` : ""}`);
}

class ClientError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

// --- Page pool ---

interface Waiter {
  resolve: (page: Page) => void;
  reject: (err: Error) => void;
  timer: NodeJS.Timeout;
}

class PagePool {
  private readonly idle: Page[] = [];
  private readonly waiters: Waiter[] = [];
  private live = 0;
  private closed = false;

  constructor(
    private readonly context: BrowserContext,
    private readonly size: number,
  ) {}

  get ready(): boolean {
    return !this.closed && this.live === this.size;
  }

  async start(): Promise<void> {
    await Promise.all(Array.from({ length: this.size }, () => this.spawn()));
  }

  private async spawn(): Promise<void> {
    const page = await this.context.newPage();
    try {
      page.setDefaultTimeout(RENDER_TIMEOUT_MS);
      await page.goto(`${ORIGIN}index.html`, { waitUntil: "load" });
      await page.waitForFunction(() => window.ygg?.ready === true);
      await page.evaluate((src) => window.ygg.render("markdown", src, 4), WARMUP_MARKDOWN);
      await page.evaluate(() => window.ygg.reset());
    } catch (err) {
      await page.close().catch(() => {});
      throw err;
    }
    this.live += 1;
    this.release(page);
  }

  /** Replace a page that errored or timed out; retries until it succeeds or the pool closes. */
  async recycle(page: Page): Promise<void> {
    this.live -= 1;
    await page.close().catch(() => {});
    for (let attempt = 1; !this.closed; attempt++) {
      try {
        await this.spawn();
        return;
      } catch (err) {
        log("page spawn failed", { attempt, error: String(err) });
        await sleep(Math.min(5000, 250 * attempt));
      }
    }
  }

  acquire(): Promise<Page> {
    const page = this.idle.pop();
    if (page) return Promise.resolve(page);
    const { promise, resolve, reject } = Promise.withResolvers<Page>();
    const waiter: Waiter = {
      resolve,
      reject,
      timer: setTimeout(() => {
        this.waiters.splice(this.waiters.indexOf(waiter), 1);
        reject(new ClientError(503, "渲染服务繁忙，请稍后再试。"));
      }, QUEUE_TIMEOUT_MS),
    };
    this.waiters.push(waiter);
    return promise;
  }

  release(page: Page): void {
    if (this.closed) {
      void page.close().catch(() => {});
      return;
    }
    const waiter = this.waiters.shift();
    if (waiter) {
      clearTimeout(waiter.timer);
      waiter.resolve(page);
    } else {
      this.idle.push(page);
    }
  }

  close(): void {
    this.closed = true;
    for (const waiter of this.waiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.reject(new ClientError(503, "渲染服务正在重启，请稍后再试。"));
    }
  }
}

// --- Rendering ---

interface Image {
  png: string;
  width: number;
  height: number;
}

/** The render page's files, keyed by URL path (e.g. "/chunks/x.js"); loaded once so routing never touches disk. */
async function loadAssets(): Promise<Map<string, { body: Buffer; contentType: string }>> {
  const assets = new Map<string, { body: Buffer; contentType: string }>();
  for (const entry of await readdir(PAGE_DIR, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile()) continue;
    const file = path.join(entry.parentPath, entry.name);
    const urlPath = `/${path.relative(PAGE_DIR, file).split(path.sep).join("/")}`;
    assets.set(urlPath, {
      body: await readFile(file),
      contentType: MIME[path.extname(file)] ?? "application/octet-stream",
    });
  }
  return assets;
}

/**
 * Context-wide route: fulfills exact asset paths on ORIGIN, aborts everything else. Must never throw: a rejection
 * from a route handler is unhandled and would take down the process.
 */
function assetRoute(assets: Map<string, { body: Buffer; contentType: string }>) {
  return async (route: Route): Promise<void> => {
    const url = route.request().url();
    try {
      // Compare the raw (still percent-encoded) path: bundle file names never need escaping.
      const asset = url.startsWith(ORIGIN) ? assets.get(new URL(url).pathname) : undefined;
      if (!asset) {
        log("blocked request", { url: url.slice(0, 200) });
        await route.abort("blockedbyclient");
        return;
      }
      if (LOG_REQUESTS) log("asset", { path: url.slice(ORIGIN.length - 1) });
      await route.fulfill({ status: 200, body: asset.body, contentType: asset.contentType });
    } catch (err) {
      // Typically the page was closed (recycled) while the request was in flight.
      log("asset route failed", { url: url.slice(0, 200), error: err instanceof Error ? err.message : String(err) });
      await route.abort("failed").catch(() => {});
    }
  };
}

interface RenderResult {
  images: Image[];
  warnings: string[];
}

async function renderOn(page: Page, kind: Kind, source: string, maxImages: number): Promise<RenderResult> {
  const outcome: RenderOutcome = await page.evaluate(([k, s, m]) => window.ygg.render(k, s, m), [
    kind,
    source,
    maxImages,
  ] as const);
  if (!outcome.ok) throw new ClientError(422, outcome.error);
  const images: Image[] = [];
  for (const clip of outcome.shots) {
    const png = await page.screenshot({ clip, fullPage: true, type: "png", animations: "disabled", caret: "hide" });
    // PNG IHDR: width and height are big-endian u32 at bytes 16 and 20.
    images.push({ png: png.toString("base64"), width: png.readUInt32BE(16), height: png.readUInt32BE(20) });
  }
  return { images, warnings: outcome.warnings };
}

async function render(pool: PagePool, kind: Kind, source: string, maxImages: number): Promise<RenderResult> {
  const page = await pool.acquire();
  const timeout = Promise.withResolvers<never>();
  const timer = setTimeout(
    () => timeout.reject(new ClientError(503, `渲染超时（${RENDER_TIMEOUT_MS / 1000} 秒），请简化内容后重试。`)),
    RENDER_TIMEOUT_MS,
  );
  const work = renderOn(page, kind, source, maxImages);
  // After a timeout the page is recycled and the abandoned work rejects; nobody awaits it any more.
  work.catch(() => {});
  try {
    const result = await Promise.race([work, timeout.promise]);
    pool.release(page);
    return result;
  } catch (err) {
    if (err instanceof ClientError && err.status === 422) {
      pool.release(page);
    } else {
      void pool.recycle(page);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

// --- HTTP ---

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY_BYTES) throw new ClientError(400, "request body too large");
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function parseRequest(raw: string): { kind: Kind; source: string; maxImages: number } {
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    throw new ClientError(400, "body must be JSON");
  }
  if (typeof body !== "object" || body === null) throw new ClientError(400, "body must be a JSON object");
  const { kind, source, max_images: maxImages = MAX_IMAGES } = body as Record<string, unknown>;
  if (typeof kind !== "string" || !Object.hasOwn(KINDS, kind)) {
    throw new ClientError(400, "kind must be one of markdown, mermaid, html");
  }
  if (typeof source !== "string") throw new ClientError(400, "source must be a string");
  if (typeof maxImages !== "number" || !Number.isInteger(maxImages) || maxImages < 1 || maxImages > MAX_IMAGES) {
    throw new ClientError(400, `max_images must be an integer in 1..${MAX_IMAGES}`);
  }
  if (!source.trim()) throw new ClientError(422, "内容为空，没有可渲染的东西。");
  if (source.length > MAX_SOURCE_CHARS) {
    throw new ClientError(422, `内容太长（${source.length} 字符），上限 ${MAX_SOURCE_CHARS} 字符，请精简后重试。`);
  }
  return { kind: kind as Kind, source, maxImages };
}

function send(res: ServerResponse, status: number, body: unknown): void {
  // Called from catch blocks too: must not throw (an escaped error would be an unhandled rejection).
  if (res.headersSent || res.destroyed) return;
  const data = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(data) });
  res.end(data);
}

async function main(): Promise<void> {
  const browser: Browser = await chromium.launch({ args: ["--disable-dev-shm-usage"] });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    deviceScaleFactor: 2,
    locale: "zh-CN",
    serviceWorkers: "block",
    acceptDownloads: false,
  });
  await context.route("**/*", assetRoute(await loadAssets()));
  await context.routeWebSocket(/.*/, (ws) => {
    log("blocked websocket", { url: ws.url().slice(0, 200) });
    ws.close().catch(() => {});
  });

  const pool = new PagePool(context, POOL_SIZE);
  const started = performance.now();
  await pool.start();
  log("page pool ready", { size: POOL_SIZE, ms: Math.round(performance.now() - started) });

  let inflight = 0;
  let shuttingDown = false;

  const server = createServer((req, res) => {
    const url = req.url ?? "/";
    if (req.method === "GET" && url === "/healthz") {
      const ok = !shuttingDown && pool.ready && browser.isConnected();
      send(res, ok ? 200 : 503, { status: ok ? "ok" : "unavailable" });
      return;
    }
    if (url !== "/render") {
      send(res, 404, { error: "not found" });
      return;
    }
    if (req.method !== "POST") {
      send(res, 405, { error: "method not allowed" });
      return;
    }
    if (shuttingDown) {
      send(res, 503, { error: "渲染服务正在重启，请稍后再试。" });
      return;
    }
    inflight += 1;
    const t0 = performance.now();
    let kind = "?";
    void (async () => {
      try {
        const request = parseRequest(await readBody(req));
        kind = request.kind;
        const result = await render(pool, request.kind, request.source, request.maxImages);
        send(res, 200, result);
        log("render", {
          kind,
          chars: request.source.length,
          images: result.images.map((i) => `${i.width}x${i.height}`).join(","),
          ms: Math.round(performance.now() - t0),
        });
      } catch (err) {
        const status = err instanceof ClientError ? err.status : 500;
        const message = err instanceof ClientError ? err.message : "渲染失败，请稍后重试。";
        send(res, status, { error: message });
        log("render failed", { kind, status, error: err instanceof Error ? err.message : String(err) });
      } finally {
        inflight -= 1;
      }
    })();
  });
  server.listen(HTTP_PORT, "0.0.0.0", () => {
    log("listening", { http: HTTP_PORT });
  });

  const shutdown = async (signal: string) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log("shutting down", { signal, inflight });
    server.close();
    const deadline = Date.now() + RENDER_TIMEOUT_MS;
    while (inflight > 0 && Date.now() < deadline) await sleep(50);
    pool.close();
    await browser.close().catch(() => {});
    process.exit(0);
  };
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
  process.on("SIGINT", () => void shutdown("SIGINT"));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
