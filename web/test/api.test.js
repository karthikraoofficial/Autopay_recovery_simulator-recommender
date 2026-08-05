// The blank-page regression, tested at the point where it actually went wrong.
//
// A 404 from /recommend/mappings was parsed as JSON, stored in `mappings`, and the next
// render called `.find` on an error object — which unmounts the entire React application,
// not just the section that failed. Two independent guarantees keep that from recurring:
// this file covers "a non-ok response never reaches state", and ErrorBoundary covers "even
// if something does throw, it stays inside this section".
//
// Run by `node --test`, which ships with Node. No test framework is added: the stack is
// fixed in SPEC §7 and this defect is testable without a DOM, because the fault was in
// what reached state rather than in how it rendered.

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { getJson, loadMappings } from "../src/api.js";

const URL_ = "/api/recommend/mappings";

function respond(status, body, { text = JSON.stringify(body) } = {}) {
  return async () => ({ ok: status >= 200 && status < 300, status, text: async () => text });
}

describe("loadMappings", () => {
  it("returns the profiles when the API answers", async () => {
    const result = await loadMappings(URL_, respond(200, [{ name: "example" }]));
    assert.deepEqual(result.mappings, [{ name: "example" }]);
    assert.equal(result.error, null);
  });

  it("never lets a 404 body reach state, however well it parses", async () => {
    // The exact payload that caused the blank page: valid JSON, an object, not a list.
    const result = await loadMappings(URL_, respond(404, { detail: "Not Found" }));
    assert.deepEqual(result.mappings, []);
    assert.ok(result.error);
  });

  it("explains a 404 as a stale API rather than a wrong route", async () => {
    const result = await loadMappings(URL_, respond(404, { detail: "Not Found" }));
    assert.match(result.error, /restart uvicorn/i);
  });

  it("always returns an array, whatever the API sends", async () => {
    for (const body of [{ detail: "nope" }, "a string", 42, null]) {
      const result = await loadMappings(URL_, respond(200, body));
      assert.ok(Array.isArray(result.mappings), `not an array for ${JSON.stringify(body)}`);
    }
  });

  it("never throws, so a caller cannot be unmounted by it", async () => {
    const cases = [
      respond(500, { detail: "boom" }),
      respond(200, null, { text: "" }),
      respond(200, null, { text: "<html>proxy error</html>" }),
      async () => {
        throw new TypeError("Failed to fetch");
      },
    ];
    for (const impl of cases) {
      const result = await loadMappings(URL_, impl);
      assert.deepEqual(result.mappings, []);
      assert.ok(result.error, "a failure must be reported, not swallowed");
    }
  });
});

describe("getJson", () => {
  it("rejects a non-ok response rather than returning its parsed body", async () => {
    await assert.rejects(
      () => getJson(URL_, undefined, respond(422, { detail: "bad column" })),
      /bad column/
    );
  });

  it("does not mistake a parseable error body for success", async () => {
    // `fetch(...).then(r => r.json())` returns this happily. That is the whole bug.
    await assert.rejects(() => getJson(URL_, undefined, respond(404, { detail: "Not Found" })));
  });

  it("names the API when the request never completes", async () => {
    await assert.rejects(
      () =>
        getJson(URL_, undefined, async () => {
          throw new TypeError("Failed to fetch");
        }),
      /Cannot reach the simulation API/
    );
  });
});

// --- containment -------------------------------------------------------------------------
//
// The other half of the guarantee. Above: a failed fetch cannot put a bad value in state.
// Here: even if something in the section does throw, it stays inside the section.
//
// Tested without a DOM by exercising the boundary's own contract directly -- React elements
// are plain objects, and `getDerivedStateFromError` is a pure static method. What this
// cannot assert is that the browser wires them up; that is what the structural check in
// tests/test_web.py is for.

import ErrorBoundary from "../src/ErrorBoundary.jsx";

describe("ErrorBoundary", () => {
  it("switches to a fallback rather than propagating the error upward", () => {
    const next = ErrorBoundary.getDerivedStateFromError(new Error("mappings.find is not a function"));
    assert.ok(next.error, "an error must be recorded, which is what stops it propagating");
  });

  it("renders its children untouched while nothing has failed", () => {
    const boundary = new ErrorBoundary({ children: "the upload section", section: "Upload" });
    boundary.state = { error: null };
    assert.equal(boundary.render(), "the upload section");
  });

  it("renders a named fallback instead of the children once something has", () => {
    const boundary = new ErrorBoundary({ children: "the upload section", section: "Upload" });
    boundary.state = { error: new Error("mappings.find is not a function") };
    const tree = JSON.stringify(boundary.render());
    assert.match(tree, /Upload/);
    assert.match(tree, /mappings\.find is not a function/);
    assert.match(tree, /rest of this page is unaffected/);
    assert.ok(!tree.includes("the upload section"), "the failed children must not be rendered");
  });
});
