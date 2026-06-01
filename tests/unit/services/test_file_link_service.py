"""
Unit tests for FileLinkService — turning a stored object key into a capability link.
"""
import pytest

from src.services.file_access_token_service import FileAccessTokenService
from src.services.file_link_service import FileLinkService


_SECRET = "z" * 40


@pytest.fixture
def tokens():
    return FileAccessTokenService(secret_key=_SECRET)


@pytest.fixture
def svc(tokens):
    return FileLinkService(token_service=tokens, base_url="https://dev.alekbot.app/")


class TestBuildLink:

    def test_link_shape(self, svc):
        link = svc.build_link(key="docs/abc-report.pdf", user_id="u1")
        assert link.startswith("https://dev.alekbot.app/f/")
        # base_url trailing slash must be normalized (no double slash before /f/)
        assert "//f/" not in link.replace("https://", "")

    def test_token_round_trips_to_key_and_user(self, svc, tokens):
        link = svc.build_link(key="docs/abc-report.pdf", user_id="u1")
        token = link.rsplit("/f/", 1)[1]
        decoded = tokens.verify(token)
        assert decoded.key == "docs/abc-report.pdf"
        assert decoded.user_id == "u1"

    def test_regular_doc_is_not_gated(self, svc, tokens):
        link = svc.build_link(key="docs/x.pdf", user_id="u1")
        token = link.rsplit("/f/", 1)[1]
        assert tokens.verify(token).gated is False

    def test_email_review_is_gated(self, svc, tokens):
        link = svc.build_link(key="email_review/2026-06-01.html", user_id="u1")
        token = link.rsplit("/f/", 1)[1]
        assert tokens.verify(token).gated is True

    def test_deep_research_not_gated(self, svc, tokens):
        link = svc.build_link(key="deep_research/u1/ts-report.md", user_id="u1")
        token = link.rsplit("/f/", 1)[1]
        assert tokens.verify(token).gated is False

    def test_user_upload_not_gated(self, svc, tokens):
        link = svc.build_link(key="u1/files/report.docx", user_id="u1")
        token = link.rsplit("/f/", 1)[1]
        assert tokens.verify(token).gated is False
