"""GmailProviderAdapter.list_parts - the `displayed_inline` flag, at the aiohttp boundary (tests/support/http_boundary_fake).

The flag comes from the part's own headers (Content-Disposition type as the first `;` token, Content-ID), not from where
the data sits and not from a substring of the file name. Invented Gmail payloads in tests/fixtures/email/."""
import json
from pathlib import Path

import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import creds

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "email"
CREDS = creds("user-1", "gmail", access_token="TOKEN-1")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def adapter():
    return GmailProviderAdapter("cid", "csecret")


async def _flags(monkeypatch, adapter, fixture: str, msg: str = "m1"):
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", f"{BASE}/messages/{msg}", 200, _fixture(fixture))
    parts = await adapter.list_parts(CREDS, msg)
    assert fake.unrouted == []
    return {p.filename: p.displayed_inline for p in parts}


async def test_the_flag_follows_the_part_headers(monkeypatch, adapter):
    assert await _flags(monkeypatch, adapter, "gmail_multipart_dispositions.json") == {
        "logo.png": True,                  # Content-ID + inline
        "image001.png": True,              # Content-ID, no disposition
        "LAB-2025-03-0417.pdf": False,     # Content-ID, but an attachment disposition
        "RM-55120.pdf": True,              # upper-case INLINE value
        "expenses_2025.csv": False,        # no headers at all
        "budget_2026.xlsx": True,          # lower-case header name
        "inline-notes.pdf": False,         # the word is in the file name, the type is attachment
    }


async def test_the_existing_nested_message_flags_only_its_logo(monkeypatch, adapter):
    flags = await _flags(monkeypatch, adapter, "gmail_multipart_nested.json")
    assert flags["logo.png"] is True
    assert [name for name, flag in flags.items() if flag and name != "logo.png"] == []


async def test_inline_parts_stay_listed_and_names_match_the_indexed_names(monkeypatch, adapter):
    fake = http_boundary_fake.install(monkeypatch)
    payload = _fixture("gmail_multipart_dispositions.json")
    fake.route("GET", f"{BASE}/messages/m1", 200, payload)
    listed = [p.filename for p in await adapter.list_parts(CREDS, "m1")]
    assert listed == GmailProviderAdapter._parse_payload(payload["payload"])[2]
    assert "logo.png" in listed and "image001.png" in listed
