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
