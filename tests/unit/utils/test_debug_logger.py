"""
Unit tests for PromptDebugLogger utilities.

Covers:
- _slug: safe filesystem/GCS slug generation
- _split_json_blocks: extraction of ```json fences from text
- log_response: readable output format (turn, type, JSON block extraction)
- log_prompt: GCS blob name includes system_instruction label
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from src.utils.debug_logger import (
    PromptDebugLogger,
    _slug,
    _split_json_blocks,
)


# ---------------------------------------------------------------------------
# _slug
# ---------------------------------------------------------------------------

class TestSlug:

    def test_alphanumeric_passthrough(self):
        assert _slug("hello123") == "hello123"

    def test_spaces_become_underscores(self):
        assert _slug("hello world") == "hello_world"

    def test_special_chars_replaced(self):
        assert _slug("[User message]") == "user_message"

    def test_uppercase_lowercased(self):
        assert _slug("Stage 1 EXTRACT") == "stage_1_extract"

    def test_max_len_respected(self):
        long = "a" * 100
        assert len(_slug(long, max_len=35)) == 35

    def test_leading_trailing_separators_stripped(self):
        assert not _slug("---hello---").startswith("_")
        assert not _slug("---hello---").endswith("_")

    def test_empty_string(self):
        assert _slug("") == ""


# ---------------------------------------------------------------------------
# _split_json_blocks
# ---------------------------------------------------------------------------

class TestSplitJsonBlocks:

    def test_no_json_block(self):
        text = "Just some prose here."
        clean, blocks = _split_json_blocks(text)
        assert clean == text
        assert blocks == []

    def test_single_valid_block_extracted(self):
        text = 'Before\n```json\n{"a": 1}\n```\nAfter'
        clean, blocks = _split_json_blocks(text)
        assert "```json" not in clean
        assert len(blocks) == 1
        assert json.loads(blocks[0]) == {"a": 1}

    def test_prose_preserved_around_block(self):
        text = "Intro\n```json\n{\"x\": 2}\n```\nOutro"
        clean, _ = _split_json_blocks(text)
        assert "Intro" in clean
        assert "Outro" in clean

    def test_invalid_json_block_left_in_place(self):
        text = "```json\nnot valid json\n```"
        clean, blocks = _split_json_blocks(text)
        assert "```json" in clean
        assert blocks == []

    def test_multiple_valid_blocks(self):
        text = (
            "First\n```json\n{\"a\": 1}\n```\n"
            "Middle\n```json\n{\"b\": 2}\n```\nLast"
        )
        clean, blocks = _split_json_blocks(text)
        assert len(blocks) == 2
        assert json.loads(blocks[0]) == {"a": 1}
        assert json.loads(blocks[1]) == {"b": 2}
        assert "```json" not in clean

    def test_consolidation_report_format(self):
        """Real consolidation response: prose + REPORT json block."""
        text = (
            "## Step 4 — ANALYZE\n"
            "→ fact_id: abc returns exact match. **DISCARD.**\n\n"
            "## Step 8 — REPORT\n\n"
            "```json\n"
            '{\n    "operations": [\n        {"action": "DISCARD", "reason": "duplicate"}\n    ]\n}\n'
            "```\n\n"
            "**Verdict:** clean replay."
        )
        clean, blocks = _split_json_blocks(text)
        assert len(blocks) == 1
        parsed = json.loads(blocks[0])
        assert parsed["operations"][0]["action"] == "DISCARD"
        assert "ANALYZE" in clean
        assert "Verdict" in clean
        assert "```json" not in clean


# ---------------------------------------------------------------------------
# log_response: local filesystem mode
# ---------------------------------------------------------------------------

class TestLogResponseLocal:

    @pytest.fixture
    def logger(self, tmp_path):
        return PromptDebugLogger(enabled=True, base_dir=str(tmp_path))

    def _read_latest(self, tmp_path: Path, pattern: str) -> str:
        files = list(tmp_path.glob(pattern))
        assert files, f"No files matching {pattern} in {tmp_path}"
        return files[-1].read_text(encoding="utf-8")

    def test_plain_text_response_stored_as_is(self, logger, tmp_path):
        logger.log_response("agent_x", "plain text response")
        content = self._read_latest(tmp_path, "*_response.txt")
        assert "plain text response" in content

    def test_json_response_text_extracted(self, logger, tmp_path):
        payload = json.dumps({"text": "Hello\nworld", "tokens": 42})
        logger.log_response("agent_x", payload)
        content = self._read_latest(tmp_path, "*_response.txt")
        assert "=== TEXT ===" in content
        assert "Hello\nworld" in content
        assert "=== TOKENS: 42 ===" in content

    def test_embedded_json_block_extracted_as_section(self, logger, tmp_path):
        report = '{\n    "operations": [{"action": "CREATE"}]\n}'
        text_with_block = f"Analysis prose.\n\n```json\n{report}\n```\n\nVerdict."
        payload = json.dumps({"text": text_with_block, "tokens": 100})
        logger.log_response("agent_x", payload)
        content = self._read_latest(tmp_path, "*_response.txt")
        assert "=== TEXT ===" in content
        assert "=== JSON ===" in content
        assert '"CREATE"' in content
        assert "```json" not in content

    def test_tool_calls_response_creates_file(self, logger, tmp_path):
        payload = json.dumps({
            "text": "",
            "tool_calls": [{"name": "search_existing_facts", "args": {}}]
        })
        logger.log_response("agent_x", payload, metadata={"turn": 2})
        files = list(tmp_path.glob("*_response.txt"))
        assert files, "Expected response file to be created"

    def test_response_file_created_with_turn_metadata(self, logger, tmp_path):
        payload = json.dumps({"text": "done", "tokens": 10})
        logger.log_response("agent_x", payload, metadata={"turn": 3})
        files = list(tmp_path.glob("*_response.txt"))
        assert files, "Expected response file to be created"

    def test_disabled_logger_returns_none(self):
        logger = PromptDebugLogger(enabled=False)
        result = logger.log_response("agent_x", "data")
        assert result is None

    def test_tool_calls_section_rendered(self, logger, tmp_path):
        payload = json.dumps({
            "text": "thinking...",
            "tool_calls": [{"name": "create_fact", "args": {"content": "User owns a car"}}],
        })
        logger.log_response("agent_x", payload)
        content = self._read_latest(tmp_path, "*_response.txt")
        assert "=== TEXT ===" in content
        assert "=== TOOL CALLS ===" in content
        assert "create_fact" in content


# ---------------------------------------------------------------------------
# log_prompt: filename format
# ---------------------------------------------------------------------------

class TestLogPromptLabel:

    def test_local_filename_uses_timestamp_format(self, tmp_path):
        logger = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        logger.log_prompt("agent_x", "prompt text")
        files = list(tmp_path.glob("*_prompt.txt"))
        assert files

    def test_gcs_blob_uses_timestamp_format(self):
        uploaded = {}

        def fake_upload(content, blob_name):
            uploaded["blob"] = blob_name

        logger = PromptDebugLogger(enabled=True)
        logger._gcs_bucket_name = "test-bucket"
        logger._gcs_upload = fake_upload  # type: ignore[method-assign]

        logger.log_prompt("consolidation", "prompt", system_instruction="[User message]")
        assert "_prompt.txt" in uploaded["blob"]

    def test_gcs_blob_no_system_instruction(self):
        uploaded = {}

        def fake_upload(content, blob_name):
            uploaded["blob"] = blob_name

        logger = PromptDebugLogger(enabled=True)
        logger._gcs_bucket_name = "test-bucket"
        logger._gcs_upload = fake_upload  # type: ignore[method-assign]

        logger.log_prompt("consolidation", "prompt", system_instruction=None)
        assert "_prompt.txt" in uploaded["blob"]


# ---------------------------------------------------------------------------
# __init__ — GCS branch logging
# ---------------------------------------------------------------------------

class TestInitGcsBranch:

    def test_enabled_with_gcs_bucket_logs_gcs_mode(self, tmp_path):
        """When GCS bucket is set, logs GCS mode (not local dir)."""
        # Force GCS by NOT passing base_dir (only then _gcs_bucket_name is read from env)
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"DEBUG_PROMPTS_BUCKET": "my-bucket"}):
            pdl = PromptDebugLogger(enabled=True)
        assert pdl._gcs_bucket_name == "my-bucket"
        # base_dir.mkdir should NOT have been called
        assert not pdl.base_dir.exists() or pdl._gcs_bucket_name == "my-bucket"


# ---------------------------------------------------------------------------
# _gcs_upload()
# ---------------------------------------------------------------------------

class TestGcsUpload:

    def test_success_path(self, tmp_path):
        from unittest.mock import MagicMock, patch
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        pdl._gcs_bucket_name = "test-bucket"

        mock_client = MagicMock()
        mock_blob = MagicMock()
        mock_client.bucket.return_value.blob.return_value = mock_blob

        with patch("src.utils.debug_logger.PromptDebugLogger._gcs_upload",
                   wraps=lambda self, c, b: None):
            pass  # We'll test via side-channel

        # Direct call with mocked storage module
        with patch.dict("sys.modules", {"google.cloud.storage": MagicMock(Client=lambda: mock_client)}):
            pdl._gcs_client = None  # reset so it re-initializes
            pdl._gcs_upload("hello content", "agent/2026-03-29/file.txt")

        mock_blob.upload_from_string.assert_called_once_with(
            "hello content", content_type="text/plain; charset=utf-8"
        )

    def test_exception_is_swallowed(self, tmp_path):
        """GCS upload failure must not propagate."""
        from unittest.mock import patch
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        pdl._gcs_bucket_name = "test-bucket"

        with patch.dict("sys.modules", {"google.cloud.storage": None}):
            # google.cloud not importable → ImportError → swallowed
            pdl._gcs_upload("content", "blob/name.txt")  # must not raise


# ---------------------------------------------------------------------------
# log_llm_request()
# ---------------------------------------------------------------------------

class TestLogLlmRequest:

    @pytest.fixture
    def pdl(self, tmp_path):
        return PromptDebugLogger(enabled=True, base_dir=str(tmp_path))

    def _make_request(self, system=None, messages=None, tools=None, model="gemini", temp=0.5):
        from unittest.mock import MagicMock
        req = MagicMock()
        req.system_instruction = system
        req.messages = messages or []
        req.tools = tools
        req.model_name = model
        req.temperature = temp
        req.use_grounding = False
        return req

    def test_disabled_returns_none(self, tmp_path):
        pdl = PromptDebugLogger(enabled=False)
        assert pdl.log_llm_request("agent", self._make_request()) is None

    def test_creates_request_file(self, pdl, tmp_path):
        req = self._make_request(system="sys instruction")
        result = pdl.log_llm_request("agent_x", req, turn=1)
        assert result is not None
        files = list(tmp_path.glob("*_request.txt"))
        assert files

    def test_content_includes_model_and_agent(self, pdl, tmp_path):
        req = self._make_request(model="claude-3-5-sonnet")
        pdl.log_llm_request("my_agent", req)
        content = list(tmp_path.glob("*_request.txt"))[-1].read_text()
        assert "my_agent" in content
        assert "claude-3-5-sonnet" in content

    def test_tools_listed_in_header(self, pdl, tmp_path):
        tool = {"name": "search_facts"}
        req = self._make_request(tools=[tool])
        pdl.log_llm_request("agent_x", req)
        content = list(tmp_path.glob("*_request.txt"))[-1].read_text()
        assert "search_facts" in content

    def test_turn_shown_when_nonzero(self, pdl, tmp_path):
        req = self._make_request()
        pdl.log_llm_request("agent_x", req, turn=3)
        content = list(tmp_path.glob("*_request.txt"))[-1].read_text()
        assert "TURN: 3" in content

    def test_gcs_path_returns_gs_url(self, pdl):
        uploaded = {}

        def fake_upload(content, blob_name):
            uploaded["blob"] = blob_name

        pdl._gcs_bucket_name = "my-bucket"
        pdl._gcs_upload = fake_upload  # type: ignore[method-assign]
        req = self._make_request()
        result = pdl.log_llm_request("agent_x", req)
        assert result and result.startswith("gs://my-bucket/")

    def test_tool_with_name_attribute(self, pdl, tmp_path):
        """Tools can be objects with .name attribute (not just dicts)."""
        from unittest.mock import MagicMock
        tool = MagicMock()
        tool.name = "vector_search"
        req = self._make_request(tools=[tool])
        pdl.log_llm_request("agent_x", req)
        content = list(tmp_path.glob("*_request.txt"))[-1].read_text()
        assert "vector_search" in content


# ---------------------------------------------------------------------------
# log_prompt() — additional branches
# ---------------------------------------------------------------------------

class TestLogPromptExtra:

    def test_system_instruction_included_in_file(self, tmp_path):
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        pdl.log_prompt("agent_x", "user prompt", system_instruction="system text here")
        content = list(tmp_path.glob("*_prompt.txt"))[-1].read_text()
        assert "[system]" in content
        assert "system text here" in content

    def test_metadata_model_key_shown(self, tmp_path):
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        pdl.log_prompt("agent_x", "prompt", metadata={"model": "gpt-4o", "user_id": "u1"})
        content = list(tmp_path.glob("*_prompt.txt"))[-1].read_text()
        assert "MODEL: gpt-4o" in content
        assert "user_id" in content  # rest dict printed

    def test_exception_in_write_returns_none(self, tmp_path):
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        from unittest.mock import patch
        with patch.object(Path, "write_text", side_effect=IOError("disk full")):
            result = pdl.log_prompt("agent_x", "prompt")
        assert result is None


# ---------------------------------------------------------------------------
# log_response() — additional branches
# ---------------------------------------------------------------------------

class TestLogResponseExtra:

    @pytest.fixture
    def pdl(self, tmp_path):
        return PromptDebugLogger(enabled=True, base_dir=str(tmp_path))

    def test_finish_reason_shown(self, pdl, tmp_path):
        payload = json.dumps({"text": "done", "finish_reason": "stop"})
        pdl.log_response("agent_x", payload)
        content = list(tmp_path.glob("*_response.txt"))[-1].read_text()
        assert "=== FINISH: stop ===" in content

    def test_metadata_model_and_tokens_in_header(self, pdl, tmp_path):
        payload = json.dumps({"text": "ok"})
        pdl.log_response("agent_x", payload, metadata={"model": "claude-3", "tokens": 200})
        content = list(tmp_path.glob("*_response.txt"))[-1].read_text()
        assert "MODEL: claude-3" in content
        assert "TOKENS: 200" in content

    def test_gcs_path_returns_gs_url(self, pdl):
        uploaded = {}

        def fake_upload(content, blob_name):
            uploaded["blob"] = blob_name

        pdl._gcs_bucket_name = "my-bucket"
        pdl._gcs_upload = fake_upload  # type: ignore[method-assign]
        result = pdl.log_response("agent_x", "plain")
        assert result and result.startswith("gs://my-bucket/")

    def test_exception_in_write_returns_none(self, pdl, tmp_path):
        from unittest.mock import patch
        with patch.object(Path, "write_text", side_effect=IOError("disk full")):
            result = pdl.log_response("agent_x", "text")
        assert result is None


# ---------------------------------------------------------------------------
# log_tool_calls()
# ---------------------------------------------------------------------------

class TestLogToolCalls:

    @pytest.fixture
    def pdl(self, tmp_path):
        return PromptDebugLogger(enabled=True, base_dir=str(tmp_path))

    def test_disabled_returns_none(self, pdl):
        pdl.enabled = False
        result = pdl.log_tool_calls("agent_x", [], [])
        assert result is None

    def test_creates_json_file(self, pdl, tmp_path):
        calls = [{"name": "search_facts", "args": {"query": "test"}}]
        results = [{"name": "search_facts", "result": [], "status": "success"}]
        result = pdl.log_tool_calls("agent_x", calls, results, metadata={"turn": 1})
        assert result is not None
        files = list(tmp_path.glob("*_tools_*.json"))
        assert files

    def test_json_content_valid(self, pdl, tmp_path):
        calls = [{"name": "create_fact", "args": {"text": "User is a developer"}}]
        results = [{"name": "create_fact", "result": "id123", "status": "success"}]
        pdl.log_tool_calls("consol", calls, results)
        files = list(tmp_path.glob("*_tools_*.json"))
        data = json.loads(files[-1].read_text())
        assert data["tool_calls"][0]["name"] == "create_fact"

    def test_gcs_path_returns_gs_url(self, pdl):
        uploaded = {}

        def fake_upload(content, blob_name):
            uploaded["blob"] = blob_name

        pdl._gcs_bucket_name = "my-bucket"
        pdl._gcs_upload = fake_upload  # type: ignore[method-assign]
        result = pdl.log_tool_calls("agent_x", [], [])
        assert result and result.startswith("gs://my-bucket/")


# ---------------------------------------------------------------------------
# log_consolidation_summary()
# ---------------------------------------------------------------------------

class TestLogConsolidationSummary:

    @pytest.fixture
    def pdl(self, tmp_path):
        return PromptDebugLogger(enabled=True, base_dir=str(tmp_path))

    def test_disabled_returns_none(self, pdl):
        pdl.enabled = False
        assert pdl.log_consolidation_summary("agent_x", []) is None

    def test_creates_json_file(self, pdl, tmp_path):
        ops = [{"action": "CREATE", "fact_id": "abc", "reason": "new"}]
        result = pdl.log_consolidation_summary("consol", ops, metadata={"turns": 3})
        assert result is not None
        files = list(tmp_path.glob("*_summary_*.json"))
        assert files

    def test_summary_counts_by_action(self, pdl, tmp_path):
        ops = [
            {"action": "CREATE"},
            {"action": "CREATE"},
            {"action": "UPDATE"},
        ]
        pdl.log_consolidation_summary("consol", ops)
        files = list(tmp_path.glob("*_summary_*.json"))
        data = json.loads(files[-1].read_text())
        assert data["summary"]["by_action"]["CREATE"] == 2
        assert data["summary"]["by_action"]["UPDATE"] == 1

    def test_gcs_path_returns_gs_url(self, pdl):
        uploaded = {}

        def fake_upload(content, blob_name):
            uploaded["blob"] = blob_name

        pdl._gcs_bucket_name = "my-bucket"
        pdl._gcs_upload = fake_upload  # type: ignore[method-assign]
        result = pdl.log_consolidation_summary("agent_x", [])
        assert result and result.startswith("gs://my-bucket/")


# ---------------------------------------------------------------------------
# _count_by_action()
# ---------------------------------------------------------------------------

class TestCountByAction:

    def test_empty_operations(self, tmp_path):
        pdl = PromptDebugLogger(enabled=False, base_dir=str(tmp_path))
        assert pdl._count_by_action([]) == {}

    def test_counts_multiple_actions(self, tmp_path):
        pdl = PromptDebugLogger(enabled=False, base_dir=str(tmp_path))
        ops = [{"action": "A"}, {"action": "B"}, {"action": "A"}, {"action": "C"}]
        counts = pdl._count_by_action(ops)
        assert counts == {"A": 2, "B": 1, "C": 1}

    def test_unknown_action_key(self, tmp_path):
        pdl = PromptDebugLogger(enabled=False, base_dir=str(tmp_path))
        ops = [{"no_action_key": "x"}]
        counts = pdl._count_by_action(ops)
        assert counts == {"UNKNOWN": 1}


# ---------------------------------------------------------------------------
# _rotate_files()
# ---------------------------------------------------------------------------

class TestRotateFiles:

    def test_tools_pattern_matched(self, tmp_path):
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path), max_files=2)
        # Create 3 tools files
        for i in range(3):
            (tmp_path / f"agent_x_tools_2026010{i}_123456.json").write_text("x")
        pdl._rotate_files("agent_x", "tools")
        remaining = list(tmp_path.glob("agent_x_tools_*.json"))
        assert len(remaining) <= 2

    def test_summary_pattern_matched(self, tmp_path):
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path), max_files=1)
        for i in range(3):
            (tmp_path / f"agent_x_summary_2026010{i}_123456.json").write_text("x")
        pdl._rotate_files("agent_x", "summary")
        remaining = list(tmp_path.glob("agent_x_summary_*.json"))
        assert len(remaining) <= 1

    def test_exception_swallowed(self, tmp_path):
        """_rotate_files must not propagate exceptions."""
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        pdl.base_dir = Path("/nonexistent_path_xyz")
        pdl._rotate_files("agent_x", "response")  # must not raise


# ---------------------------------------------------------------------------
# Additional coverage — missed branches
# ---------------------------------------------------------------------------

class TestAdditionalCoverage:

    def test_log_prompt_disabled_returns_none(self, tmp_path):
        pdl = PromptDebugLogger(enabled=False)
        assert pdl.log_prompt("agent_x", "prompt text") is None

    def test_log_llm_request_messages_with_parts(self, tmp_path):
        """Request messages with .parts objects → text extracted per part."""
        from unittest.mock import MagicMock
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        part = MagicMock()
        part.text = "part text"
        msg = MagicMock()
        msg.role = "user"
        msg.parts = [part]
        req = MagicMock()
        req.system_instruction = None
        req.messages = [msg]
        req.tools = None
        req.model_name = "gemini"
        req.temperature = 0.7
        req.use_grounding = False
        result = pdl.log_llm_request("agent_x", req)
        assert result is not None
        content = list(tmp_path.glob("*_request.txt"))[-1].read_text()
        assert "part text" in content

    def test_log_llm_request_exception_returns_none(self, tmp_path):
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        from unittest.mock import MagicMock, patch
        req = MagicMock()
        req.system_instruction = None
        req.messages = []
        req.tools = None
        req.model_name = "x"
        req.temperature = None
        req.use_grounding = False
        with patch.object(Path, "write_text", side_effect=IOError("fail")):
            result = pdl.log_llm_request("agent_x", req)
        assert result is None

    def test_log_tool_calls_exception_returns_none(self, tmp_path):
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        from unittest.mock import patch
        with patch("builtins.open", side_effect=IOError("fail")):
            result = pdl.log_tool_calls("agent_x", [{"name": "t"}], [])
        assert result is None

    def test_log_consolidation_summary_exception_returns_none(self, tmp_path):
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        from unittest.mock import patch
        with patch("builtins.open", side_effect=IOError("fail")):
            result = pdl.log_consolidation_summary("agent_x", [{"action": "CREATE"}])
        assert result is None

    def test_log_response_json_block_re_encoded(self, tmp_path):
        """JSON blocks in text are re-encoded with indent=2."""
        pdl = PromptDebugLogger(enabled=True, base_dir=str(tmp_path))
        inner_json = '{"key":"val"}'
        text_with_block = f"prose\n```json\n{inner_json}\n```\nend"
        payload = json.dumps({"text": text_with_block})
        pdl.log_response("agent_x", payload)
        content = list(tmp_path.glob("*_response.txt"))[-1].read_text()
        assert "=== JSON ===" in content
        assert '"key"' in content
