// Every call to the API goes through here.
//
// The rule this module exists to enforce: **a non-ok response never reaches state.** The
// blank-page bug came from a section fetching with `fetch(...).then(r => r.json())`. A 404
// body is `{"detail": "Not Found"}`, which parses perfectly well, so an error object was
// stored where a list was expected and the next render threw `mappings.find is not a
// function` — unmounting the whole application because one panel could not list profiles.
//
// Parsing succeeding is not the same as the request succeeding, and that is the mistake
// worth making structurally impossible rather than remembering not to make.

// `fetchImpl` is injectable so the failure paths can be tested without a browser or a
// running API. Nothing in the app passes it.
export async function getJson(url, options, fetchImpl = fetch) {
  let response;
  try {
    response = await fetchImpl(url, options);
  } catch {
    // fetch only rejects when the request never completed at all.
    throw new Error(
      `Cannot reach the simulation API at ${url}. Start it with: ` +
        `python -m uvicorn rebound.api.app:app --port 8001`
    );
  }
  const text = await response.text();
  if (!text) {
    throw new Error(
      `The API returned an empty response (HTTP ${response.status}) for ${url}. ` +
        "This usually means the API process is not running behind the dev-server proxy."
    );
  }
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    throw new Error(`The API returned a non-JSON response (HTTP ${response.status}) for ${url}.`);
  }
  if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status} from ${url}`);
  return body;
}

// A 404 here means the API process predates the route rather than that the route is wrong,
// which is a different thing to tell someone and the likeliest cause by far.
const STALE_API =
  "The API is running but has no /recommend/mappings route. It was most likely started " +
  "before this feature existed — restart uvicorn to pick it up.";

/**
 * The mapping profiles, or an explanation. Never throws, and never returns a non-array.
 *
 * Separated from the component so the failure path is testable without a DOM: the defect
 * was in what reached state, not in how it was rendered.
 */
export async function loadMappings(url, fetchImpl = fetch) {
  try {
    const body = await getJson(url, undefined, fetchImpl);
    if (!Array.isArray(body)) {
      // Defence against the exact shape that caused the blank page. A 200 carrying
      // something other than a list is still not a list.
      return { mappings: [], error: "The API returned mapping profiles in a shape this page cannot read." };
    }
    return { mappings: body, error: null };
  } catch (error) {
    const message = String(error?.message ?? error);
    return { mappings: [], error: /HTTP 404|Not Found/i.test(message) ? STALE_API : message };
  }
}

/**
 * The API's own account of itself, or an explanation. Never throws.
 */
export async function loadHealth(url, fetchImpl = fetch) {
  try {
    const body = await getJson(url, undefined, fetchImpl);
    return { health: body && typeof body === "object" ? body : null, error: null };
  } catch (error) {
    const message = String(error?.message ?? error);
    return {
      health: null,
      error: /HTTP 404|Not Found/i.test(message)
        ? "The API has no /health route, which means it predates this check entirely. Restart uvicorn."
        : message,
    };
  }
}

/**
 * Whether the API is running the code this page was built from.
 *
 * A pure comparison, kept out of the component so the states can be tested without a
 * browser. It reports "unknown" rather than "ok" when either side cannot be established:
 * a check that quietly passes when it cannot see is worse than no check, because it is
 * read as a guarantee.
 */
export function compareBuild(apiHealth, buildCommit) {
  const apiCommit = apiHealth?.commit ?? null;
  if (!buildCommit || !apiCommit) {
    return {
      state: "unknown",
      message:
        "Cannot tell whether the API is running this page's code: " +
        (buildCommit ? "the API did not report a commit." : "this page has no build commit."),
    };
  }
  if (apiCommit !== buildCommit) {
    return {
      state: "mismatch",
      message:
        `The API is running commit ${apiCommit.slice(0, 12)}, but this page was built ` +
        `from ${buildCommit.slice(0, 12)}. Routes added since the API started will return ` +
        `404 and features will appear broken. Restart uvicorn (use --reload to stop this ` +
        `recurring); rebuild the page if it is the one that is behind.`,
    };
  }
  if (apiHealth.dirty) {
    return {
      state: "dirty",
      message:
        `The API is on this page's commit ${apiCommit.slice(0, 12)}, but its working tree ` +
        `has uncommitted changes, so matching commits do not prove the running code matches.`,
    };
  }
  return { state: "ok", message: `API and page are both on ${apiCommit.slice(0, 12)}.` };
}
