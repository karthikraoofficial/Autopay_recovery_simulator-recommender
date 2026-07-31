from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from rebound.config import Confidence, load_assumptions

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_shipped_assumptions_file_loads() -> None:
    a = load_assumptions()
    assert a.version == 1
    assert a.assumptions


def test_value_lookup() -> None:
    assert load_assumptions().value("harness.performance_fee_rate") == 0.15


def test_unknown_key_names_itself() -> None:
    with pytest.raises(KeyError, match="nope.not.here"):
        load_assumptions().value("nope.not.here")


def test_every_estimate_is_an_open_question() -> None:
    a = load_assumptions()
    listed = {q.key for q in a.open_questions}
    for key, entry in a.assumptions.items():
        if entry.confidence is Confidence.ESTIMATE:
            assert key in listed


def test_estimate_without_open_question_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "assumptions.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "assumptions": {
                    "a.b": {"value": 1, "unit": "count", "source": "x", "confidence": "estimate"}
                },
                "open_questions": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="a.b"):
        load_assumptions(path)


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "assumptions.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "assumptions": {
                    "a.b": {
                        "value": 1,
                        "unit": "count",
                        "source": "x",
                        "confidence": "primary",
                        "sauce": "typo",
                    }
                },
                "open_questions": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_assumptions(path)


def test_default_path_points_at_the_repo_config() -> None:
    from rebound.config import DEFAULT_ASSUMPTIONS_PATH

    assert DEFAULT_ASSUMPTIONS_PATH == REPO_ROOT / "config" / "assumptions.yaml"
    assert DEFAULT_ASSUMPTIONS_PATH.is_file()
