"""
Building the drive specialist for one user (docs/10_rfcs/DRIVE_SPECIALIST_RFC.md §5.1, §5.5): the one place that
turns a user's config and their DriveOperations into a DriveAgent. UserAgentFactory's lazy builder calls it, and so do
the drive evals (`scripts/validation/drive_eval/`), which build the specialist over the fixture drive exactly as
production builds it over the user's.

Public interface:
    build_drive_specialist(user_id, *, context_builder, prompt_builder, user_config, operations, alert_sink)
        -> DriveAgent

Provider, tier and fallback come from AgentContextBuilder ("drive": claude BALANCED, failover openai PERFORMANCE);
thinking is the user's `agent_thinking["drive"]` when set (DriveAgent applies DRIVE.thinking_effort, "medium",
otherwise). `alert_sink` is the ops sink the consolidation agent gets: it takes the one 🚨 line of a turn-cap exit
without a report. The drive state (connected, search on or off) is read by the agent through `operations.preflight`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ..agents.drive_agent import DriveAgent
from ..domain.agent import AgentConfig
from ..infrastructure.agent_config import DRIVE
from ..infrastructure.agent_manifest import DRIVE_SPECIALIST

if TYPE_CHECKING:
    from ..domain.user import UserBotConfig
    from ..infrastructure.drive_operations import DriveOperations
    from ..ports.alert_sink import AlertSinkPort


def build_drive_specialist(user_id: str, *, context_builder: Any, prompt_builder: Any, user_config: "UserBotConfig",
                           operations: "DriveOperations", alert_sink: Optional["AlertSinkPort"]) -> DriveAgent:
    return DriveAgent(
        config=AgentConfig(
            agent_id=f"{DRIVE_SPECIALIST.agent_id}_{user_id}",
            agent_type=DRIVE_SPECIALIST.agent_type,
            timeout_ms=DRIVE.timeout_ms,
            capabilities=["drive"],
        ),
        execution_context=context_builder.build(DRIVE_SPECIALIST.agent_type, user_config),
        operations=operations,
        prompt_builder=prompt_builder,
        user_timezone=user_config.timezone,
        thinking=user_config.get_thinking_for_agent(DRIVE_SPECIALIST.agent_type),
        alert_sink=alert_sink,
    )
