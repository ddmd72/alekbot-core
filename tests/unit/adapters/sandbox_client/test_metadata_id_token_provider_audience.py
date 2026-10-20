"""An empty audience must never build a token provider (T8b carry from the T8 review)."""
import pytest

from src.adapters.sandbox_client.metadata_id_token_provider import MetadataIdTokenProvider


@pytest.mark.parametrize("audience", ["", "   ", "/", " / "])
def test_empty_audience_is_rejected_at_construction(audience):
    with pytest.raises(ValueError):
        MetadataIdTokenProvider(audience)


def test_non_empty_audience_is_accepted():
    assert MetadataIdTokenProvider("https://sandbox.example/").audience == "https://sandbox.example"
