"""`safe_return_path`: the post-sign-in target is a same-site relative path or the Cabinet."""
import pytest

from src.domain.safe_redirect import DEFAULT_RETURN_PATH, safe_return_path


@pytest.mark.parametrize("path", [
    "/cabinet",
    "/cabinet/call",
    "/join?code=X",
    "/mcp/consent?req=JWT&x=1",
    "/files/abc?token=a%2Fb&x=1#frag",
])
def test_relative_paths_are_kept_with_their_query(path):
    assert safe_return_path(path) == path


@pytest.mark.parametrize("value", [
    "https://evil.example/phish",
    "http://evil.example",
    "//evil.example/x",
    "///evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "/ok\\..\\evil",
    "javascript:alert(1)",
    "evil.example/x",
    "cabinet",
    " /cabinet",
    "/ca\nbinet",
    "/ca\rbinet",
    "/ca\tbinet",
    "/ca\x00binet",
    "/ca\x7fbinet",
    "",
    None,
])
def test_anything_else_becomes_the_cabinet(value):
    assert safe_return_path(value) == DEFAULT_RETURN_PATH == "/cabinet"
