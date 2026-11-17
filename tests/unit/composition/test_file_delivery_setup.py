"""`build_file_delivery`: with a media bucket, one link service over the one token service feeds the document delivery,
and both link kinds point at the service URL; without one, only the token service exists."""
import dataclasses

import pytest

from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.composition.file_delivery_setup import build_file_delivery
from src.config.environment import EnvironmentConfig
from tests.support.firestore_eval import EvalDb

SECRET = "token-secret-" + "x" * 32
URL = "https://svc.example.test"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    return EnvironmentConfig()


def test_with_a_bucket_the_links_and_the_delivery_share_one_token_service_and_one_media_adapter(env):
    delivery = build_file_delivery({"GCS_MEDIA_BUCKET": "media-1"}, EvalDb(), env, token_secret=SECRET, service_url=URL)

    assert isinstance(delivery.media_storage, GcsMediaAdapter) and delivery.media_storage._bucket_name == "media-1"
    assert delivery.file_token_service._secret == SECRET
    assert delivery.link_service._tokens is delivery.file_token_service and delivery.link_service._base_url == URL
    assert delivery.short_link_service._base_url == URL
    assert delivery.doc_delivery_service._storage is delivery.media_storage
    assert delivery.doc_delivery_service._links is delivery.link_service


@pytest.mark.parametrize("config", [{}, {"GCS_MEDIA_BUCKET": ""}])
def test_without_a_bucket_only_the_token_service_exists(env, config):
    delivery = build_file_delivery(config, EvalDb(), env, token_secret=SECRET, service_url=URL)

    assert delivery.file_token_service._secret == SECRET
    assert (delivery.media_storage, delivery.link_service, delivery.short_link_service,
            delivery.doc_delivery_service) == (None, None, None, None)


def test_the_bundle_is_frozen(env):
    delivery = build_file_delivery({}, EvalDb(), env, token_secret=SECRET, service_url=URL)

    with pytest.raises(dataclasses.FrozenInstanceError):
        delivery.link_service = None  # type: ignore[misc]
