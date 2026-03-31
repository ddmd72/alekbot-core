"""
Debug Logger for Agent Prompts
================================

Centralized debug logging utility for saving agent prompts and LLM responses.
Controlled via DEBUG_PROMPTS environment variable.

Features:
- Environment-controlled (off by default in production)
- GCS backend when DEBUG_PROMPTS_BUCKET is set (Cloud Run mode)
- Local filesystem fallback for local development
- Automatic file rotation (local mode only, keeps last N files)
- Structured output with metadata
- Safe for concurrent use

Session 2026-02-16: Added tool call logging for Deliberate Fact Management
Session 2026-03-01: Added GCS backend (DEBUG_PROMPTS_BUCKET env var)
"""

import os
import re
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Dict, List
from .logger import logger


def _slug(text: str, max_len: int = 35) -> str:
    """Turn arbitrary text into a safe GCS/filesystem slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text)
    s = s.strip("_").lower()
    return s[:max_len]


_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def _split_json_blocks(text: str) -> tuple[str, list[str]]:
    """Strip ```json ... ``` fences from text.

    Returns (text_without_fences, list_of_raw_json_strings).
    Fences with invalid JSON are left in place unchanged.
    """
    extracted: list[str] = []

    def _replacer(m: re.Match) -> str:
        raw = m.group(1).strip()
        try:
            json.loads(raw)      # validate only; keep raw for re-indent below
            extracted.append(raw)
            return ""            # remove fence from prose
        except json.JSONDecodeError:
            return m.group(0)   # not valid JSON — leave as-is

    clean = _JSON_FENCE_RE.sub(_replacer, text)
    return clean.strip(), extracted


class PromptDebugLogger:
    """
    Debug logger for agent prompts and LLM responses.

    When DEBUG_PROMPTS_BUCKET is set → writes to GCS (Cloud Run mode).
    When not set → writes to local filesystem (local dev mode).

    Usage:
        debug_logger = PromptDebugLogger()
        debug_logger.log_prompt("smart_agent", prompt, metadata={"user": "123"})
        debug_logger.log_response("smart_agent", response, metadata={"tokens": 1000})
    """

    def __init__(
        self,
        enabled: Optional[bool] = None,
        base_dir: str = "debug_prompts",
        max_files: int = 20
    ):
        """
        Initialize debug logger.

        Args:
            enabled: Override for DEBUG_PROMPTS env var (None = read from env)
            base_dir: Directory to store debug files (local mode only)
            max_files: Maximum number of files to keep per agent (local mode only)
        """
        if enabled is None:
            enabled = os.getenv("DEBUG_PROMPTS", "false").lower() == "true"

        self.enabled = enabled
        self.base_dir = Path(base_dir)
        self.max_files = max_files
        # Use GCS only when no explicit base_dir was provided; explicit base_dir → local mode.
        self._gcs_bucket_name: Optional[str] = (
            None if base_dir != "debug_prompts" else os.getenv("DEBUG_PROMPTS_BUCKET")
        )
        self._gcs_client = None

        if self.enabled:
            if self._gcs_bucket_name:
                logger.info(
                    f"🔍 [PromptDebugLogger] Enabled → GCS bucket: {self._gcs_bucket_name}"
                )
            else:
                self.base_dir.mkdir(exist_ok=True)
                logger.info(
                    f"🔍 [PromptDebugLogger] Enabled → local dir: {base_dir} (max_files={max_files})"
                )

    def _gcs_upload(self, content: str, blob_name: str) -> None:
        """Upload content to GCS. Failures are non-fatal (warning only)."""
        try:
            from google.cloud import storage  # lazy import
            if self._gcs_client is None:
                self._gcs_client = storage.Client()
            bucket = self._gcs_client.bucket(self._gcs_bucket_name)
            blob = bucket.blob(blob_name)
            blob.upload_from_string(content, content_type="text/plain; charset=utf-8")
            logger.info(f"🔍 [PromptDebugLogger] GCS upload: gs://{self._gcs_bucket_name}/{blob_name}")
        except Exception as e:
            logger.warning(f"⚠️ [PromptDebugLogger] GCS upload failed ({blob_name}): {e}")
    
    def log_llm_request(
        self,
        agent_name: str,
        request: Any,
        turn: int = 0,
    ) -> Optional[str]:
        """Log the full LLMRequest as a JSON messages array (real API payload).

        Args:
            agent_name: Name of the agent
            request:    LLMRequest domain object
            turn:       Turn number (0 = single-turn agents)

        Returns:
            Path to log file if successful, None otherwise
        """
        if not self.enabled:
            return None

        try:
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")

            messages = []
            if getattr(request, "system_instruction", None):
                messages.append({"role": "system", "content": request.system_instruction})
            for msg in getattr(request, "messages", []):
                role = getattr(msg, "role", "user")
                parts = getattr(msg, "parts", [])
                text_parts = [getattr(p, "text", "") for p in parts if getattr(p, "text", None)]
                content = "\n".join(text_parts) if text_parts else str(parts)
                messages.append({"role": role, "content": content})

            payload: Dict[str, Any] = {
                "model": getattr(request, "model_name", ""),
                "temperature": getattr(request, "temperature", None),
                "thinking": getattr(request, "thinking", None),
                "use_grounding": getattr(request, "use_grounding", False),
                "messages": messages,
            }
            tool_names: List[str] = []
            if getattr(request, "tools", None):
                for t in request.tools:
                    name = (
                        t.get("name") if isinstance(t, dict)
                        else getattr(t, "name", None)
                    )
                    if name:
                        tool_names.append(name)

            header_lines = [
                "=" * 80,
                f"AGENT: {agent_name}",
                f"TIMESTAMP: {now.isoformat()}",
                f"MODEL: {payload['model']}",
                f"temperature: {payload['temperature']}  use_grounding: {payload['use_grounding']}",
            ]
            if turn:
                header_lines.append(f"TURN: {turn}")
            if tool_names:
                header_lines.append(f"tools: [{', '.join(tool_names)}]")
            header_lines.append("=" * 80)

            body_lines = []
            for msg in messages:
                body_lines.append(f"\n[{msg['role']}]")
                body_lines.append(msg["content"])

            content = "\n".join(header_lines) + "\n".join(body_lines)

            if self._gcs_bucket_name:
                blob_name = f"{agent_name}/{now.strftime('%Y-%m-%d')}/{timestamp}_request.txt"
                self._gcs_upload(content, blob_name)
                return f"gs://{self._gcs_bucket_name}/{blob_name}"
            else:
                filename = f"{timestamp}_request.txt"
                filepath = self.base_dir / filename
                filepath.write_text(content, encoding="utf-8")
                self._rotate_files(agent_name, "request")
                logger.info(f"🔍 [PromptDebugLogger] Saved request: {filepath}")
                return str(filepath)

        except Exception as e:
            logger.warning(f"⚠️ [PromptDebugLogger] Failed to log request: {e}")
            return None

    def log_prompt(
        self,
        agent_name: str,
        prompt: str,
        metadata: Optional[Dict[str, Any]] = None,
        system_instruction: Optional[str] = None
    ) -> Optional[str]:
        """
        Log agent prompt to file.

        Args:
            agent_name: Name of the agent (e.g., "smart_response_agent")
            prompt: The prompt text
            metadata: Optional metadata (user_id, session_id, etc.)
            system_instruction: Optional system instruction

        Returns:
            Path to log file if successful, None otherwise
        """
        if not self.enabled:
            return None

        try:
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d_%H-%M-%S") + f"_{now.microsecond // 1000:03d}"
            filename = f"{timestamp}_prompt.txt"

            lines = []
            lines.append("=" * 80)
            lines.append(f"AGENT: {agent_name}")
            lines.append(f"TIMESTAMP: {now.isoformat()}")
            if metadata:
                if "model" in metadata:
                    lines.append(f"MODEL: {metadata['model']}")
                rest = {k: v for k, v in metadata.items() if k != "model"}
                if rest:
                    lines.append(f"METADATA: {rest}")
            lines.append("=" * 80)
            lines.append("")
            if system_instruction:
                lines.append("[system]")
                lines.append(system_instruction)
                lines.append("")
            lines.append("[user]")
            lines.append(prompt)
            content = "\n".join(lines)

            if self._gcs_bucket_name:
                blob_name = f"{agent_name}/{now.strftime('%Y-%m-%d')}/{timestamp}_prompt.txt"
                self._gcs_upload(content, blob_name)
                return f"gs://{self._gcs_bucket_name}/{blob_name}"
            else:
                filepath = self.base_dir / filename
                filepath.write_text(content, encoding="utf-8")
                self._rotate_files(agent_name, "prompt")
                logger.info(f"🔍 [PromptDebugLogger] Saved prompt: {filepath}")
                return str(filepath)

        except Exception as e:
            logger.warning(f"⚠️ [PromptDebugLogger] Failed to log prompt: {e}")
            return None
    
    def log_response(
        self,
        agent_name: str,
        response: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Log LLM response to file.
        
        Args:
            agent_name: Name of the agent
            response: The LLM response text
            metadata: Optional metadata (tokens, duration, etc.)
            
        Returns:
            Path to log file if successful, None otherwise
        """
        if not self.enabled:
            return None

        try:
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d_%H-%M-%S") + f"_{now.microsecond // 1000:03d}"

            turn = (metadata or {}).get("turn", 0)

            # Parse JSON response to extract readable text
            text_body = response
            try:
                data = json.loads(response)
                tool_calls = data.get("tool_calls") or []
                sections = []
                if data.get("text"):
                    prose, json_blocks = _split_json_blocks(data["text"])
                    sections.append("=== TEXT ===")
                    sections.append(prose)
                    for raw in json_blocks:
                        sections.append("=== JSON ===")
                        try:
                            sections.append(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
                        except json.JSONDecodeError:
                            sections.append(raw)
                if tool_calls:
                    sections.append("=== TOOL CALLS ===")
                    sections.append(json.dumps(tool_calls, ensure_ascii=False, indent=2))
                if data.get("finish_reason"):
                    sections.append(f"=== FINISH: {data['finish_reason']} ===")
                if data.get("tokens"):
                    sections.append(f"=== TOKENS: {data['tokens']} ===")
                text_body = "\n\n".join(sections) if sections else response
            except (json.JSONDecodeError, Exception):
                logger.debug("Could not parse structured response for debug formatting, using raw string")

            filename = f"{timestamp}_response.txt"

            lines = []
            lines.append("=" * 80)
            lines.append(f"AGENT: {agent_name}")
            lines.append(f"TIMESTAMP: {now.isoformat()}")
            if metadata:
                if "model" in metadata:
                    lines.append(f"MODEL: {metadata['model']}")
                if "tokens" in metadata:
                    lines.append(f"TOKENS: {metadata['tokens']}")
                rest = {k: v for k, v in metadata.items() if k not in ("model", "tokens")}
                if rest:
                    lines.append(f"METADATA: {rest}")
            lines.append("=" * 80)
            lines.append("")
            lines.append(text_body)
            content = "\n".join(lines)

            if self._gcs_bucket_name:
                blob_name = f"{agent_name}/{now.strftime('%Y-%m-%d')}/{timestamp}_response.txt"
                self._gcs_upload(content, blob_name)
                return f"gs://{self._gcs_bucket_name}/{blob_name}"
            else:
                filepath = self.base_dir / filename
                filepath.write_text(content, encoding="utf-8")
                self._rotate_files(agent_name, "response")
                logger.info(f"🔍 [PromptDebugLogger] Saved response: {filepath}")
                return str(filepath)

        except Exception as e:
            logger.warning(f"⚠️ [PromptDebugLogger] Failed to log response: {e}")
            return None
    
    def log_tool_calls(
        self,
        agent_name: str,
        tool_calls: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Log tool calls and their results (for ConsolidationAgent v3).
        
        Args:
            agent_name: Name of the agent (e.g., "consolidation_v3")
            tool_calls: List of tool calls with {name, args}
            tool_results: List of tool results with {name, result, status}
            metadata: Optional metadata (turn number, total operations, etc.)
            
        Returns:
            Path to log file if successful, None otherwise
            
        Example:
            debug_logger.log_tool_calls(
                "consolidation_v3",
                tool_calls=[{"name": "search_existing_facts", "args": {...}}],
                tool_results=[{"name": "search_existing_facts", "result": [...], "status": "success"}],
                metadata={"turn": 1, "user_id": "abc123"}
            )
        """
        if not self.enabled:
            return None

        try:
            now = datetime.now()
            timestamp = now.strftime("%Y%m%d_%H%M%S")

            log_data = {
                "agent": agent_name,
                "timestamp": now.isoformat(),
                "metadata": metadata or {},
                "tool_calls": tool_calls,
                "tool_results": tool_results
            }

            if self._gcs_bucket_name:
                blob_name = f"{agent_name}/{now.strftime('%Y-%m-%d')}/tools_{timestamp}.json"
                self._gcs_upload(json.dumps(log_data, indent=2, ensure_ascii=False), blob_name)
                return f"gs://{self._gcs_bucket_name}/{blob_name}"
            else:
                filename = f"{agent_name}_tools_{timestamp}.json"
                filepath = self.base_dir / filename
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(log_data, f, indent=2, ensure_ascii=False)
                self._rotate_files(agent_name, "tools")
                logger.info(
                    f"🔍 [PromptDebugLogger] Saved tool calls: {filepath} "
                    f"({len(tool_calls)} calls, {len(tool_results)} results)"
                )
                return str(filepath)

        except Exception as e:
            logger.warning(f"⚠️  [PromptDebugLogger] Failed to log tool calls: {e}")
            return None
    
    def log_consolidation_summary(
        self,
        agent_name: str,
        operations: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Log final consolidation summary (Step 8: REPORT from v3 prompt).
        
        Args:
            agent_name: Name of the agent
            operations: List of operations performed
            metadata: Optional metadata (duration, tokens, etc.)
            
        Returns:
            Path to log file if successful, None otherwise
            
        Example:
            debug_logger.log_consolidation_summary(
                "consolidation_v3",
                operations=[
                    {"action": "UPDATE", "fact_id": "xyz", "reason": "..."},
                    {"action": "CREATE", "fact_id": "abc", "reason": "..."}
                ],
                metadata={"total_turns": 5, "duration_ms": 45000}
            )
        """
        if not self.enabled:
            return None

        try:
            now = datetime.now()
            timestamp = now.strftime("%Y%m%d_%H%M%S")

            summary_data = {
                "agent": agent_name,
                "timestamp": now.isoformat(),
                "metadata": metadata or {},
                "operations": operations,
                "summary": {
                    "total_operations": len(operations),
                    "by_action": self._count_by_action(operations)
                }
            }

            if self._gcs_bucket_name:
                blob_name = f"{agent_name}/{now.strftime('%Y-%m-%d')}/summary_{timestamp}.json"
                self._gcs_upload(json.dumps(summary_data, indent=2, ensure_ascii=False), blob_name)
                return f"gs://{self._gcs_bucket_name}/{blob_name}"
            else:
                filename = f"{agent_name}_summary_{timestamp}.json"
                filepath = self.base_dir / filename
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(summary_data, f, indent=2, ensure_ascii=False)
                self._rotate_files(agent_name, "summary")
                logger.info(
                    f"🔍 [PromptDebugLogger] Saved consolidation summary: {filepath} "
                    f"({len(operations)} operations)"
                )
                return str(filepath)

        except Exception as e:
            logger.warning(f"⚠️  [PromptDebugLogger] Failed to log summary: {e}")
            return None
    
    def _count_by_action(self, operations: List[Dict[str, Any]]) -> Dict[str, int]:
        """Count operations by action type."""
        counts: Dict[str, int] = {}
        for op in operations:
            action = op.get("action", "UNKNOWN")
            counts[action] = counts.get(action, 0) + 1
        return counts
    
    def _rotate_files(self, agent_name: str, file_type: str) -> None:
        """
        Rotate log files to keep only the most recent N files.
        
        Args:
            agent_name: Agent name to filter files
            file_type: "prompt", "response", "tools", or "summary"
        """
        try:
            # Find all matching files
            if file_type == "tools":
                pattern = f"{agent_name}_tools_*.json"
            elif file_type == "summary":
                pattern = f"{agent_name}_summary_*.json"
            else:
                pattern = f"*_{file_type}*.txt"
            
            files = sorted(self.base_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
            
            # Remove oldest files if over limit
            while len(files) > self.max_files:
                oldest = files.pop(0)
                oldest.unlink()
                logger.debug(f"🗑️  [PromptDebugLogger] Rotated old file: {oldest.name}")
                
        except Exception as e:
            logger.warning(f"⚠️  [PromptDebugLogger] File rotation failed: {e}")


# Global instance (lazy initialized)
_global_logger: Optional[PromptDebugLogger] = None


def get_debug_logger() -> PromptDebugLogger:
    """Get global debug logger instance (singleton)."""
    global _global_logger
    if _global_logger is None:
        _global_logger = PromptDebugLogger()
    return _global_logger
