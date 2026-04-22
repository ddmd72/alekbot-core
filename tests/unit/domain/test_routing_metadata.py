from src.domain.agent import RoutingMetadata
from src.domain.task_complexity import TaskComplexity


def test_routing_metadata_to_from_dict_roundtrip():
    metadata = RoutingMetadata(
        user_tone="friendly",
        task_complexity=TaskComplexity.INFO_SEARCH,
        needs_tools=["memory_search"],
        reasoning="Single fact lookup"
    )

    payload = metadata.to_dict()
    restored = RoutingMetadata.from_dict(payload)

    assert restored.user_tone == "friendly"
    assert restored.task_complexity == TaskComplexity.INFO_SEARCH
    assert payload["task_complexity"] == "info_search"
    assert restored.needs_tools == ["memory_search"]
    assert restored.reasoning == "Single fact lookup"


def test_routing_metadata_defaults():
    restored = RoutingMetadata.from_dict({})

    assert restored.user_tone == "friendly"
    assert restored.task_complexity == TaskComplexity.SIMPLE_ANALYTICS
    assert restored.needs_tools == []
    assert restored.reasoning == ""


def test_routing_metadata_unknown_complexity_falls_back():
    """Router may emit an unrecognised string — safety net → SIMPLE_ANALYTICS."""
    restored = RoutingMetadata.from_dict({"task_complexity": "nonsense_tier"})
    assert restored.task_complexity == TaskComplexity.SIMPLE_ANALYTICS