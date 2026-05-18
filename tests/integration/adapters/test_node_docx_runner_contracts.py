"""
Integration tests for NodeDocxRunner contracts.

Third non-LLM application of the CapturingStub + ContractRule pattern (R18.2).
Demonstrates the pattern handles a subprocess boundary — distinct from LLM SDK
kwargs (claude/gemini/openai/grok), HTTP requests (gmail), and chained-query
SDK (firestore).
"""
import pytest

from src.adapters.node_docx_runner import NodeDocxRunner
from tests.contracts.adapter_contracts import (
    NODE_DOCX_INVOKED_WITH_NODE_AND_SCRIPT_PATH,
    NODE_DOCX_SPEC_PASSED_VIA_STDIN,
)
from tests.integration.adapters.conftest import NodeSubprocessCapturingStub


_JS_CODE = "process.stdout.write('hi')"
_SPEC_JSON = '{"title":"Test","sections":[{"heading":"X"}]}'


@pytest.mark.asyncio
async def test_node_docx_runner_passes_spec_json_via_stdin(monkeypatch):
    """Spec payload must reach Node via stdin, never as argv.

    Argv is bounded by E2BIG on the OS; large specs would crash silently in
    production. Stdin is unbounded.
    """
    stub = NodeSubprocessCapturingStub().install(monkeypatch, "src.adapters.node_docx_runner")
    runner = NodeDocxRunner()

    await runner.run(js_code=_JS_CODE, spec_json=_SPEC_JSON, timeout=10)

    assert len(stub.exec_calls) == 1
    NODE_DOCX_SPEC_PASSED_VIA_STDIN.validate("node_docx_runner", {
        "exec_args": stub.exec_calls[0]["args"],
        "stdin_inputs": stub.communicate_inputs,
        "expected_spec_bytes": _SPEC_JSON.encode("utf-8"),
    })


@pytest.mark.asyncio
async def test_node_docx_runner_invokes_node_with_script_in_generator_dir(monkeypatch):
    """Subprocess must invoke `node <docx_generator/...js>` for node_modules resolution."""
    stub = NodeSubprocessCapturingStub().install(monkeypatch, "src.adapters.node_docx_runner")
    runner = NodeDocxRunner()

    await runner.run(js_code=_JS_CODE, spec_json=_SPEC_JSON, timeout=10)

    NODE_DOCX_INVOKED_WITH_NODE_AND_SCRIPT_PATH.validate("node_docx_runner", {
        "exec_args": stub.exec_calls[0]["args"],
        "expected_dir_substring": "docx_generator",
    })


@pytest.mark.asyncio
async def test_node_docx_runner_returns_subprocess_stdout(monkeypatch):
    """Sanity: stdout bytes are returned to the caller; stderr is logged but ignored on rc=0."""
    stub = NodeSubprocessCapturingStub(
        stdout=b"<<PRETEND_DOCX>>",
        stderr=b"warning: deprecated API",
    ).install(monkeypatch, "src.adapters.node_docx_runner")
    runner = NodeDocxRunner()

    result = await runner.run(js_code=_JS_CODE, spec_json=_SPEC_JSON, timeout=10)

    assert result == b"<<PRETEND_DOCX>>"
    assert len(stub.exec_calls) == 1
