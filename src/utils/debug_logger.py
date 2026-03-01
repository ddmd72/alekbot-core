"""
Debug Logger for Agent Prompts
================================

Centralized debug logging utility for saving agent prompts and LLM responses.
Controlled via DEBUG_PROMPTS environment variable.

Features:
- Environment-controlled (off by default in production)
- Automatic file rotation (keeps last N files)
- Structured output with metadata
- Safe for concurrent use

Session 2026-02-16: Added tool call logging for Deliberate Fact Management
"""

import os
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from .logger import logger


class PromptDebugLogger:
    """
    Debug logger for agent prompts and LLM responses.
    
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
            base_dir: Directory to store debug files
            max_files: Maximum number of files to keep per agent
        """
        if enabled is None:
            enabled = os.getenv("DEBUG_PROMPTS", "false").lower() == "true"
        
        self.enabled = enabled
        self.base_dir = Path(base_dir)
        self.max_files = max_files
        
        if self.enabled:
            self.base_dir.mkdir(exist_ok=True)
            logger.info(
                f"🔍 [PromptDebugLogger] Enabled (max_files={max_files}, dir={base_dir})"
            )
    
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
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{agent_name}_prompt_{timestamp}.txt"
            filepath = self.base_dir / filename
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("=" * 80 + "\n")
                f.write(f"AGENT: {agent_name}\n")
                f.write(f"TIMESTAMP: {datetime.now().isoformat()}\n")

                if metadata:
                    if "model" in metadata:
                        f.write(f"MODEL: {metadata['model']}\n")
                    rest = {k: v for k, v in metadata.items() if k != "model"}
                    if rest:
                        f.write(f"METADATA: {rest}\n")

                f.write("=" * 80 + "\n\n")

                if system_instruction:
                    f.write("=== SYSTEM INSTRUCTION ===\n")
                    f.write(system_instruction)
                    f.write("\n\n")

                f.write("=== PROMPT ===\n")
                f.write(prompt)
            
            self._rotate_files(agent_name, "prompt")
            
            logger.info(f"🔍 [PromptDebugLogger] Saved prompt: {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.warning(f"⚠️  [PromptDebugLogger] Failed to log prompt: {e}")
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
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{agent_name}_response_{timestamp}.txt"
            filepath = self.base_dir / filename
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("=" * 80 + "\n")
                f.write(f"AGENT: {agent_name}\n")
                f.write(f"TIMESTAMP: {datetime.now().isoformat()}\n")

                if metadata:
                    if "model" in metadata:
                        f.write(f"MODEL: {metadata['model']}\n")
                    if "tokens" in metadata:
                        f.write(f"TOKENS: {metadata['tokens']}\n")
                    rest = {k: v for k, v in metadata.items() if k not in ("model", "tokens")}
                    if rest:
                        f.write(f"METADATA: {rest}\n")

                f.write("=" * 80 + "\n\n")
                f.write("=== LLM RESPONSE ===\n")
                f.write(response)
            
            self._rotate_files(agent_name, "response")
            
            logger.info(f"🔍 [PromptDebugLogger] Saved response: {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.warning(f"⚠️  [PromptDebugLogger] Failed to log response: {e}")
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
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{agent_name}_tools_{timestamp}.json"
            filepath = self.base_dir / filename
            
            log_data = {
                "agent": agent_name,
                "timestamp": datetime.now().isoformat(),
                "metadata": metadata or {},
                "tool_calls": tool_calls,
                "tool_results": tool_results
            }
            
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
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{agent_name}_summary_{timestamp}.json"
            filepath = self.base_dir / filename
            
            summary_data = {
                "agent": agent_name,
                "timestamp": datetime.now().isoformat(),
                "metadata": metadata or {},
                "operations": operations,
                "summary": {
                    "total_operations": len(operations),
                    "by_action": self._count_by_action(operations)
                }
            }
            
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
                pattern = f"{agent_name}_{file_type}_*.txt"
            
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
