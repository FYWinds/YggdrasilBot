// Builds dist/server.js (Node) and dist/page/ (the static render page served to Chromium from disk).
import { copyFile, mkdir, rm } from "node:fs/promises";
import { build } from "esbuild";

await rm("dist", { recursive: true, force: true });
await mkdir("dist/page", { recursive: true });

await Promise.all([
  build({
    entryPoints: ["src/server.ts"],
    outfile: "dist/server.js",
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node22",
    packages: "external",
    logLevel: "warning",
  }),
  build({
    entryPoints: { main: "src/page/main.ts", style: "src/page/style.css" },
    outdir: "dist/page",
    bundle: true,
    splitting: true,
    format: "esm",
    platform: "browser",
    target: "chrome130",
    minify: true,
    chunkNames: "chunks/[name]-[hash]",
    assetNames: "assets/[name]-[hash]",
    loader: { ".woff2": "file", ".woff": "file", ".ttf": "file" },
    legalComments: "none",
    logLevel: "warning",
  }),
  copyFile("src/page/index.html", "dist/page/index.html"),
]);
