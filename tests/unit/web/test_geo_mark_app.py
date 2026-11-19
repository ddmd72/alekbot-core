"""`GET|POST /geo/<id>` over the production `GeoMarkService` (GEO_MARKS_RFC §4.5): the real link store and session
store on the evaluating Firestore fake, the real task queue over the Cloud Tasks client fake, the real localization.
A refused request leaves the link in place; only a valid fix takes it, once."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest
from quart import Quart

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.domain.geo_mark import LINK_TTL_S, GeoLinkBinding
from src.domain.ui_messages import UIMessage
from src.locales import uk
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.reverse_geocoding_port import ReverseGeocodingPort
from src.services.geo_mark_service import GeoMarkService
from src.web.geo_mark_app import BODY_CAP_BYTES, create_geo_mark_blueprint, script_csp
from tests.support.fact_review_world import ReviewSessionStore
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue

LINKS = "t_geo_mark_links"
SESSION = "usr-1:C1"
ADDRESS = "12 Oak Street, Springfield"
GOOD = {"latitude": 41.1, "longitude": -8.6, "accuracy": 40}
JSON = {"Content-Type": "application/json"}
UNKNOWN = "AbCdEfGhIjKlMnOpQrSt_-"


class Geocoder(ReverseGeocodingPort):
    def __init__(self, address: Optional[str]) -> None:
        self.address = address

    async def nearest_address(self, latitude: float, longitude: float, *, language: str) -> Optional[str]:
        return self.address


class Channel:
    def __init__(self) -> None:
        self.texts: List[str] = []

    async def send_message(self, text: str, thread_id: Optional[str] = None, link_list=None) -> None:
        self.texts.append(text)


class Channels(NotificationChannelFactoryPort):
    def __init__(self) -> None:
        self.channel = Channel()

    def create(self, platform: str, channel_id: str):  # type: ignore[override]
        return self.channel


class Users:
    async def get_user(self, user_id: str):
        return SimpleNamespace(config=SimpleNamespace(timezone="UTC"))


class Reads:
    """The link store, counting reads: a wrong-shaped id must reach no store."""

    def __init__(self, inner: FirestoreEphemeralStore) -> None:
        self.inner, self.reads = inner, 0

    async def set(self, *args, **kwargs):
        return await self.inner.set(*args, **kwargs)

    async def get(self, *args, **kwargs):
        self.reads += 1
        return await self.inner.get(*args, **kwargs)

    async def get_and_delete(self, *args, **kwargs):
        self.reads += 1
        return await self.inner.get_and_delete(*args, **kwargs)


class World:
    def __init__(self, address: Optional[str] = ADDRESS) -> None:
        self.db = EvalDb()
        self.links = Reads(FirestoreEphemeralStore(self.db, LINKS))
        self.sessions = ReviewSessionStore(self.db, "t_")
        self.channels = Channels()
        self.queue, self.tasks = make_task_queue()
        self.service = GeoMarkService(
            links=self.links, geocoder=Geocoder(address), sessions=self.sessions, channels=self.channels,  # type: ignore[arg-type]
            task_queue=self.queue, users=Users(), localization=FileLocalizationAdapter(), digest_key=b"g" * 32,  # type: ignore[arg-type]
            base_url="https://app.example", clock=lambda: datetime(2026, 11, 18, 18, 40, tzinfo=timezone.utc))
        self.app = Quart(__name__)
        self.app.register_blueprint(create_geo_mark_blueprint(self.service))
        self.client = self.app.test_client()

    async def issue(self, text: str = "", language: str = "en") -> str:
        url = await self.service.issue_link(GeoLinkBinding(
            user_id="usr-1", account_id="acc-1", platform="slack", channel_id="C1", session_id=SESSION,
            platform_user_id="U1", language=language, text=text))
        return url.rsplit("/", 1)[1]

    def stored(self) -> Dict[str, dict]:
        return self.db.collection(LINKS).docs

    async def history(self) -> List[str]:
        return [p.text for m in await self.sessions.messages(SESSION) for p in m.parts]

    async def post(self, link_id: str, body=None, headers=None, raw: Optional[bytes] = None):
        return await self.client.post(f"/geo/{link_id}", headers=JSON if headers is None else headers,
                                      data=raw if raw is not None else json.dumps(GOOD if body is None else body))


@pytest.fixture
def world() -> World:
    return World()


class TestPage:

    async def test_a_live_link_shows_the_page_with_the_privacy_headers_and_keeps_the_record(self, world):
        link = await world.issue()
        response = await world.client.get(f"/geo/{link}")
        html = (await response.get_data()).decode()
        assert response.status_code == 200
        assert response.mimetype == "text/html"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Robots-Tag"] == "noindex"
        assert response.headers["Permissions-Policy"] == "geolocation=(self)"
        assert response.headers["Content-Security-Policy"] == script_csp(html)
        assert "getCurrentPosition" in html and link not in html
        assert len(world.stored()) == 1

    async def test_the_page_carries_the_owners_language(self, world):
        link = await world.issue(language="uk")
        html = (await (await world.client.get(f"/geo/{link}")).get_data()).decode()
        assert '<html lang="uk">' in html
        assert uk.UI_STRINGS[UIMessage.GEO_PAGE_RETRY.value] in html

    async def test_a_get_twice_leaves_the_link_usable(self, world):
        link = await world.issue()
        assert (await world.client.get(f"/geo/{link}")).status_code == 200
        assert (await world.client.get(f"/geo/{link}")).status_code == 200
        assert (await world.post(link)).status_code == 200

    async def test_a_wrong_shaped_id_is_a_404_without_a_store_read(self, world):
        for bad in ("short", "x" * 23, "AbCdEfGhIjKlMnOpQr.%20"):
            assert (await world.client.get(f"/geo/{bad}")).status_code == 404
            assert (await world.post(bad)).status_code == 404
        assert world.links.reads == 0

    async def test_an_unknown_link_is_a_404_with_the_safety_headers(self, world):
        response = await world.client.get(f"/geo/{UNKNOWN}")
        assert response.status_code == 404
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "no longer valid" in (await response.get_data()).decode()

    async def test_an_expired_link_is_a_404(self, world):
        link = await world.issue()
        for doc in world.stored().values():
            doc["expires_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
        response = await world.client.get(f"/geo/{link}")
        assert response.status_code == 404

    async def test_a_script_tag_in_a_string_cannot_end_the_data_block(self, world):
        link = await world.issue()
        world.service.page_strings = lambda language: {"geo_page_retry": "</script><b>&"}  # type: ignore[method-assign]
        html = (await (await world.client.get(f"/geo/{link}")).get_data()).decode()
        assert "</script><b>" not in html
        assert "\\u003c/script\\u003e" in html


class TestFix:

    async def test_a_valid_fix_is_saved_once_and_answers_the_address(self, world):
        link = await world.issue()
        response = await world.post(link)
        assert response.status_code == 200
        assert await response.get_json() == {"status": "saved", "address": ADDRESS, "accuracy_m": 40.0}
        assert len(world.channels.channel.texts) == 1
        assert len(await world.history()) == 2
        assert world.stored() == {}

    async def test_a_fix_with_no_address_answers_null(self):
        world = World(address=None)
        link = await world.issue()
        assert await (await world.post(link)).get_json() == {"status": "saved", "address": None, "accuracy_m": 40.0}

    async def test_a_fix_without_accuracy_is_saved(self, world):
        link = await world.issue()
        response = await world.post(link, {"latitude": 41.1, "longitude": -8.6})
        assert response.status_code == 200
        assert (await response.get_json())["accuracy_m"] is None

    async def test_a_question_is_queued_as_a_turn(self, world):
        link = await world.issue(text="Is the Iron Temple gym open?")
        response = await world.post(link)
        assert response.status_code == 200
        assert (await response.get_json())["status"] == "turn_queued"
        [(task_type, body)] = world.tasks.worker_tasks()
        assert task_type == "geo_mark_turn" and body["text"] == "Is the Iron Temple gym open?"

    async def test_the_second_post_after_the_take_is_gone(self, world):
        link = await world.issue()
        assert (await world.post(link)).status_code == 200
        response = await world.post(link)
        assert response.status_code == 410
        assert await response.get_json() == {"status": "gone"}
        assert len(world.channels.channel.texts) == 1

    async def test_an_unknown_id_is_gone(self, world):
        assert (await world.post(UNKNOWN)).status_code == 410

    async def test_a_foreign_origin_is_refused_and_the_link_survives(self, world):
        link = await world.issue()
        for origin in ("https://evil.example", "null"):
            response = await world.post(link, headers={**JSON, "Origin": origin})
            assert response.status_code == 403
        assert len(world.stored()) == 1

    async def test_this_hosts_origin_is_accepted(self, world):
        link = await world.issue()
        response = await world.post(link, headers={**JSON, "Origin": "http://localhost"})
        assert response.status_code == 200

    async def test_a_form_content_type_is_refused_and_the_link_survives(self, world):
        link = await world.issue()
        response = await world.post(link, headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert response.status_code == 415
        assert len(world.stored()) == 1

    async def test_a_body_over_the_cap_is_refused_and_the_link_survives(self, world):
        link = await world.issue()
        response = await world.post(link, raw=json.dumps({**GOOD, "pad": "x" * BODY_CAP_BYTES}).encode())
        assert response.status_code == 413
        assert len(world.stored()) == 1

    async def test_bad_coordinates_are_a_400_and_the_link_survives(self, world):
        link = await world.issue()
        bad = [{"latitude": 91, "longitude": 0}, {"latitude": 41.1, "longitude": 181}, {"latitude": "41.1", "longitude": 1},
               {"latitude": True, "longitude": 1}, {"longitude": 1}, {**GOOD, "accuracy": -1}, [1, 2]]
        for body in bad:
            assert (await world.post(link, body)).status_code == 400
        assert (await world.post(link, raw=b"{not json")).status_code == 400
        assert (await world.post(link, raw=b"\xff\xfe")).status_code == 400
        assert len(world.stored()) == 1
        assert world.channels.channel.texts == []

    async def test_a_fix_whose_history_append_fails_is_a_500_with_the_not_saved_status(self, world):
        link = await world.issue()

        async def broken(*args, **kwargs):
            raise RuntimeError("firestore down")

        world.sessions.append_messages_batch = broken  # type: ignore[method-assign]
        response = await world.post(link)
        assert response.status_code == 500
        assert await response.get_json() == {"status": "not_saved"}


def test_the_link_lives_ten_minutes():
    assert LINK_TTL_S == 600
