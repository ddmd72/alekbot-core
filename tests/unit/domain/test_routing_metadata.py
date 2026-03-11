from src.domain.agent import RoutingMetadata


def test_routing_metadata_to_from_dict_roundtrip():
    metadata = RoutingMetadata(
        user_tone="friendly",
        complexity_score=4,
        confidence=0.88,
        needs_tools=["memory_search"],
        reasoning="Single fact lookup"
    )

    payload = metadata.to_dict()
    restored = RoutingMetadata.from_dict(payload)

    assert restored.user_tone == "friendly"
    assert restored.complexity_score == 4
    assert restored.confidence == 0.88
    assert restored.needs_tools == ["memory_search"]
    assert restored.reasoning == "Single fact lookup"


def test_routing_metadata_defaults():
    restored = RoutingMetadata.from_dict({})

    assert restored.user_tone == "friendly"
    assert restored.complexity_score == 5
    assert restored.confidence == 0.5
    assert restored.needs_tools == []
    assert restored.reasoning == ""