"""The front-end guarantees that a Python suite can hold, plus the JS suite itself.

One section of the page failing must not take the page with it. That was not true: a 404
from `/recommend/mappings` was parsed as JSON, stored where a list was expected, and the
next render threw — unmounting the simulator, the headline and the exports along with the
upload panel that failed.

Two independent things stop it recurring, and they are tested at different levels:

1. **A non-ok response never reaches state** — `web/test/api.test.js`, run below.
2. **Even if something throws, it stays in its section** — the boundary's own contract is
   covered in that file too; that it is actually *wired up* is structural, and checked here
   because it is a fact about `App.jsx`'s shape rather than about any function's behaviour.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
APP = WEB / "src" / "App.jsx"
INGEST = WEB / "src" / "Ingest.jsx"


def test_the_upload_section_is_inside_an_error_boundary() -> None:
    """The wiring the JS tests cannot see. `<Ingest />` unwrapped is the original defect."""
    source = APP.read_text(encoding="utf-8")
    boundary = source.index("<ErrorBoundary")
    ingest = source.index("<Ingest />")
    closing = source.index("</ErrorBoundary>")
    assert boundary < ingest < closing, "Ingest must render inside the boundary"


def test_the_simulator_sections_are_outside_that_boundary() -> None:
    """Containment is only useful if the rest of the page is not inside it too.

    If the boundary ever grows to wrap the result or the assumptions panel, a failure in
    the upload section would blank them again — the exact outcome it was added to prevent.
    """
    source = APP.read_text(encoding="utf-8")
    contained = source[source.index("<ErrorBoundary") : source.index("</ErrorBoundary>")]
    for section in ("<Inputs", "<Result", "<Downloads", "<Assumptions"):
        assert section not in contained, f"{section} must not be inside the upload boundary"


def _code(path: Path) -> str:
    """Source with `//` comments dropped, so a comment *describing* the old defect does not
    read as the defect. The comments here quote the bad pattern deliberately."""
    return "\n".join(line.split("//")[0] for line in path.read_text(encoding="utf-8").splitlines())


def test_the_ingest_section_does_not_parse_a_response_before_checking_it() -> None:
    """`fetch(...).then(r => r.json())` is the shape that caused the blank page: a 404 body
    parses perfectly well, so the error object lands in state and the next render throws."""
    code = _code(INGEST)
    assert ".then((r) => r.json())" not in code
    assert ".json()" not in code, "parse via the checked helpers, not directly"


def test_fetched_state_starts_at_a_value_the_render_path_can_handle() -> None:
    source = INGEST.read_text(encoding="utf-8")
    assert "useState([])" in source, "the mapping list must start as an empty list"


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not on PATH")
def test_the_front_end_suite_passes() -> None:
    """Runs `npm test` in web/, so the JS guarantees are part of `pytest -q` rather than
    something a person has to remember to run separately."""
    result = subprocess.run(
        ["npm", "test", "--silent"],
        cwd=WEB,
        capture_output=True,
        text=True,
        shell=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout
