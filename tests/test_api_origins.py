"""CORS origins: the dev server always, a deployed front end only when named in the env."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rebound.api.app import ALLOWED_ORIGINS_ENV, DEV_ORIGINS, allowed_origins, create_app

DEPLOYED = "https://rebound.vercel.app"


def test_unset_is_dev_origins_only() -> None:
    assert allowed_origins(None) == list(DEV_ORIGINS)


def test_single_origin_is_added_trimmed() -> None:
    assert allowed_origins(f"  {DEPLOYED} ") == [*DEV_ORIGINS, DEPLOYED]


def test_multiple_origins_keep_their_order() -> None:
    other = "https://preview.example.com"
    assert allowed_origins(f"{DEPLOYED},{other}") == [*DEV_ORIGINS, DEPLOYED, other]


def test_blank_entries_are_ignored() -> None:
    assert allowed_origins(f" , {DEPLOYED},, ,") == [*DEV_ORIGINS, DEPLOYED]
    assert allowed_origins("") == list(DEV_ORIGINS)


def test_env_origin_reaches_the_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, DEPLOYED)
    client = TestClient(create_app())
    allowed = client.get("/health", headers={"Origin": DEPLOYED})
    other = client.get("/health", headers={"Origin": "https://elsewhere.example.com"})
    assert allowed.headers.get("access-control-allow-origin") == DEPLOYED
    assert "access-control-allow-origin" not in other.headers
