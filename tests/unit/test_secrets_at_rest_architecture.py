"""Architecture guards of secrets at rest (SECRETS_AT_REST_RFC §8 Architecture, MCP part; invariants I2, I3).

- The only `CipherPort` implementer in `src/` is the KMS adapter: no runtime holds a no-op, passthrough or test cipher
  (I3). Found by AST over every class of `src/`, so a new implementer anywhere fails here.
- The MCP store requires a cipher and a digest key: keyword-only, no default (a store built without them cannot exist).
- `CipherPort.aad` is keyword-only with no default on both methods (I2).
- The port imports nothing beyond the stdlib (`abc`): it names no provider.
"""
import ast
import inspect
from pathlib import Path
from typing import Set

import pytest

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.composition.mcp_setup import build_mcp_components
from src.ports.cipher_port import CipherPort

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def _base_names(node: ast.ClassDef) -> Set[str]:
    names = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _cipher_implementers() -> Set[str]:
    found = set()
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and "CipherPort" in _base_names(node):
                found.add(f"{path.relative_to(ROOT).as_posix()}::{node.name}")
    return found


def test_the_kms_adapter_is_the_only_cipher_in_src():
    assert _cipher_implementers() == {"src/adapters/gcp_kms_cipher_adapter.py::GcpKmsCipherAdapter"}


@pytest.mark.parametrize("name", ["cipher", "digest_key"])
def test_the_mcp_store_requires_a_cipher_and_a_digest_key(name):
    param = inspect.signature(FirestoreMCPClientRepository.__init__).parameters[name]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is inspect.Parameter.empty


@pytest.mark.parametrize("name", ["cipher", "digest_key"])
def test_the_mcp_build_requires_them_from_its_caller(name):
    param = inspect.signature(build_mcp_components).parameters[name]
    assert param.default is inspect.Parameter.empty


@pytest.mark.parametrize("method", ["encrypt", "decrypt"])
def test_aad_is_keyword_only_with_no_default(method):
    param = inspect.signature(getattr(CipherPort, method)).parameters["aad"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is inspect.Parameter.empty


def test_the_port_names_no_provider():
    tree = ast.parse((SRC / "ports" / "cipher_port.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {"abc"}


def test_the_mcp_store_is_built_with_the_graphs_cipher_in_main():
    """main.py hands the composition root's cipher and digest key to the MCP build, never a cipher of its own."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "build_mcp_components"]
    assert len(calls) == 1
    kwargs = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kwargs["cipher"] == "app.cipher" and kwargs["digest_key"] == "app.digest_key"
