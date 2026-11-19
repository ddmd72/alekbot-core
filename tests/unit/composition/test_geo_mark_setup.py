"""build_geo_mark_service (GEO_MARKS_RFC §4.3, §4.4): the service over the `geo_mark_links` collection, with the Google
geocoder only when `GOOGLE_SEARCH_API_KEY` is set, and no service without a task queue."""
from datetime import datetime, timezone
from types import SimpleNamespace

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.google_geocoding_adapter import GEOCODE_URL
from src.composition.geo_mark_setup import build_geo_mark_service
from src.config.environment import EnvironmentConfig
from src.domain.geo_mark import GeoFix, GeoLinkBinding, GeoSource
from tests.support import http_boundary_fake
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue

FIX = GeoFix.parse(41.1, -8.6, 5, datetime(2026, 11, 18, 18, 40, tzinfo=timezone.utc), GeoSource.SLACK_LINK)
ENV = EnvironmentConfig()


def _build(config, db=None, queue="make"):
    task_queue = make_task_queue()[0] if queue == "make" else queue
    return build_geo_mark_service(
        config, db or EvalDb(), ENV, digest_key=b"g" * 32, service_url="https://app.example",
        session_store=SimpleNamespace(), channel_factory=SimpleNamespace(), task_queue=task_queue,
        user_repo=SimpleNamespace(), localization_port=FileLocalizationAdapter())


async def test_with_the_key_marks_get_the_google_address(monkeypatch):
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", GEOCODE_URL, 200, {"status": "OK", "results": [{"formatted_address": "Main Street, Springfield"}]})

    service = _build({"GOOGLE_SEARCH_API_KEY": "geo-key-test"})

    assert service is not None
    assert (await service.locate(FIX, "en")).address == "Main Street, Springfield"
    assert fake.requests[0].params["key"] == "geo-key-test"


async def test_without_the_key_marks_have_no_address_and_nothing_is_requested(monkeypatch):
    fake = http_boundary_fake.install(monkeypatch)

    service = _build({"GOOGLE_SEARCH_API_KEY": None})

    assert service is not None
    assert (await service.locate(FIX, "en")).address is None
    assert fake.requests == []


async def test_links_live_in_the_geo_mark_links_collection():
    db = EvalDb()
    service = _build({}, db=db)
    assert service is not None

    url = await service.issue_link(GeoLinkBinding(user_id="usr-1", account_id="acc-1", platform="slack",
                                                  channel_id="C1", session_id="usr-1:C1", platform_user_id="U1",
                                                  language="en", text=""))

    assert url.startswith("https://app.example/geo/")
    assert ENV.geo_mark_links_collection == f"{ENV.firestore_collection_prefix}geo_mark_links"
    assert len(db.collection(ENV.geo_mark_links_collection).docs) == 1


def test_without_a_task_queue_there_is_no_service():
    assert _build({"GOOGLE_SEARCH_API_KEY": "geo-key-test"}, queue=None) is None
