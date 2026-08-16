import { execSync } from "node:child_process";

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The commit this bundle was built from, stamped in at build time so the page can compare
// itself against the commit /health reports. Null where git cannot answer -- an unknown
// build commit disables the comparison rather than faking one, because a fabricated hash
// would produce a mismatch warning that is itself wrong.
function buildCommit() {
  try {
    return execSync("git rev-parse HEAD", { encoding: "utf8" }).trim();
  } catch {
    return null;
  }
}

export default defineConfig({
  plugins: [react()],
  define: { __BUILD_COMMIT__: JSON.stringify(buildCommit()) },
  server: {
    port: 5173,
    // The API runs on 8001. Proxying keeps the front end origin-relative, so nothing in
    // the app hardcodes a host. Moved off 8000 because another project on this machine
    // owns that port; the number matters to nothing but this line and the uvicorn command.
    proxy: { "/api": { target: "http://127.0.0.1:8001", rewrite: (p) => p.replace(/^\/api/, "") } },
  },
});
