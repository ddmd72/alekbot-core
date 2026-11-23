"""Nothing in `src/` (or a root entry point) reads the Cabinet's access cookie except the one verifier,
`src/web/cabinet_auth.py` (CABINET_DEVICE_SESSIONS_RFC §7.1 "The gate", §10 "A new route reads the session cookie
itself").

A route that read the cookie and verified the JWT itself would let a revoked device through: only the verifier asks
`DeviceSessionService.is_live`. A read is `<anything>.cookies.get(<name>)` or `<anything>.cookies[<name>]` whose name is
the cookie's literal (the current `__Host-access` or the pre-session `access_token`) or the `ACCESS_COOKIE` constant;
and the literal `__Host-access` itself may appear nowhere else, so it cannot be smuggled into a read through a local
name. The detector is checked against planted reads, and against the verifier itself, so it cannot pass vacuously.
"""
import ast
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parents[3]
VERIFIER = Path("src/web/cabinet_auth.py")
COOKIE_NAMES = {"__Host-access", "access_token"}


def _names_the_cookie(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in COOKIE_NAMES
    if isinstance(node, ast.Name):
        return node.id == "ACCESS_COOKIE"
    if isinstance(node, ast.Attribute):
        return node.attr == "ACCESS_COOKIE"
    return False


def _is_cookies(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "cookies"


def cookie_reads(source: str) -> List[int]:
    """Line numbers of the access-cookie reads and of every `__Host-access` literal in `source`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and _is_cookies(node.func.value) and node.args and _names_the_cookie(node.args[0])):
            found.append(node.lineno)
        elif isinstance(node, ast.Subscript) and _is_cookies(node.value) and _names_the_cookie(node.slice):
            found.append(node.lineno)
        elif isinstance(node, ast.Constant) and node.value == "__Host-access":
            found.append(node.lineno)
    return sorted(set(found))


def _sources() -> List[Path]:
    return sorted([*ROOT.joinpath("src").rglob("*.py"), *ROOT.glob("*.py")])


def test_no_module_but_the_verifier_reads_the_access_cookie():
    offenders = []
    for path in _sources():
        rel = path.relative_to(ROOT)
        if rel == VERIFIER:
            continue
        lines = cookie_reads(path.read_text(encoding="utf-8"))
        offenders += [f"{rel}:{line}" for line in lines]
    assert offenders == [], f"read the access cookie through CabinetSessionVerifier only: {offenders}"


def test_the_verifier_is_where_the_cookie_is_read():
    assert cookie_reads(ROOT.joinpath(VERIFIER).read_text(encoding="utf-8")), "the detector finds the verifier's reads"


@pytest.mark.parametrize("planted", [
    'token = request.cookies.get("access_token")',
    'token = request.cookies.get("__Host-access", "")',
    'token = request.cookies["__Host-access"]',
    'token = request.cookies.get(ACCESS_COOKIE)',
    'token = request.cookies.get(cabinet_auth.ACCESS_COOKIE)',
    'NAME = "__Host-access"',
])
def test_the_detector_catches_a_planted_read(planted):
    assert cookie_reads(planted) == [1]


@pytest.mark.parametrize("innocent", [
    'state = request.cookies.get("oauth_state")',
    'secret = request.cookies.get(REFRESH_COOKIE)',
    'token = payload["access_token"]',
])
def test_the_detector_ignores_other_cookies_and_other_access_tokens(innocent):
    assert cookie_reads(innocent) == []
