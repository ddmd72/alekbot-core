"""render_docx_child (plan G1, G5): body framing, workdir files, node wiring."""
import struct

import pytest

from src.adapters.sandbox_engine import render_docx_child as child
from src.adapters.sandbox_engine.render_docx_child import (
    EXIT_BAD_REQUEST, BadBody, frame_body, split_body,
)


def test_split_roundtrip():
    assert split_body(frame_body(b"console.log(1)", b'{"a":1}')) == (b"console.log(1)", b'{"a":1}')


def test_length_past_body_is_bad_request():
    with pytest.raises(BadBody):
        split_body(struct.pack(">I", 50) + b"short")
    with pytest.raises(BadBody):
        split_body(b"\x00\x00")   # not even a header


def test_zero_length_script():
    assert split_body(frame_body(b"", b"{}")) == (b"", b"{}")


def test_big_spec():
    spec = b"x" * (5 * 1024 * 1024)
    assert split_body(frame_body(b"s", spec)) == (b"s", spec)


def test_unreadable_body_exits_bad_request(tmp_path):
    body = tmp_path / "body"
    body.write_bytes(struct.pack(">I", 99) + b"x")
    assert child.main([str(body), str(tmp_path)]) == EXIT_BAD_REQUEST
    assert child.main([str(tmp_path / "missing"), str(tmp_path)]) == EXIT_BAD_REQUEST
    assert child.main(["only-one-arg"]) == EXIT_BAD_REQUEST


def test_prepare_writes_script_and_spec(tmp_path):
    body = tmp_path / "body"
    body.write_bytes(frame_body(b"//user", b'{"k":2}'))
    child.prepare(str(body), str(tmp_path))
    assert (tmp_path / "spec.json").read_bytes() == b'{"k":2}'
    assert (tmp_path / "script.js").read_bytes().endswith(b"//user")


def test_node_env_is_allowlisted_and_has_node_path(monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "s3cret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = child.node_env()
    assert "SECRET_TOKEN" not in env
    assert env["NODE_PATH"].endswith("docx_generator/node_modules")
