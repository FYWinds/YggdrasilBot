import { defineConfig } from "@hey-api/openapi-ts";

// `npm run gen` reads the dumped schema (uv run python -m yggdrasil.admin.dump_openapi > web/openapi.json);
// `npm run gen:live` reads it from a running backend instead.
export default defineConfig({
  input: "./openapi.json",
  output: { path: "./src/client" },
  plugins: ["@hey-api/client-fetch", "@tanstack/react-query"],
});
