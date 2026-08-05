import React, { useEffect, useState } from "react";
import { compareBuild, loadHealth } from "./api.js";

// A stale API must announce itself.
//
// Twice it did not, and both times it was diagnosed from which paths returned 404 — which
// points at routing, the proxy, or the front end, and was wrong on both occasions. The
// process knew its own commit the whole time and had no way to say so.
//
// On the page, not in the console. A console warning is seen by someone already debugging;
// the person who needs this is the one wondering why a button does nothing.

const API = "/api";

// Stamped in by vite.config.js at build time. `null` where git could not answer, which
// disables the comparison rather than faking one.
const BUILD_COMMIT = typeof __BUILD_COMMIT__ === "string" ? __BUILD_COMMIT__ : null;

export default function BuildBanner() {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    let cancelled = false;
    loadHealth(`${API}/health`).then(({ health, error }) => {
      if (cancelled) return;
      // A /health that 404s is itself the strongest possible evidence of a stale process:
      // the route exists in every build that has this banner in it.
      setStatus(error ? { state: "mismatch", message: error } : compareBuild(health, BUILD_COMMIT));
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Silent when the API and the page agree. A banner that is always present is furniture,
  // and stops being read before the day it matters.
  if (!status || status.state === "ok") return null;

  return (
    <div className={`build-banner ${status.state}`} role="alert">
      <strong>
        {status.state === "mismatch"
          ? "The API is not running this page's code."
          : status.state === "dirty"
            ? "The API has uncommitted changes."
            : "Cannot verify the API's code."}
      </strong>{" "}
      {status.message}
    </div>
  );
}
