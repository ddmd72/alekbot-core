"""`GET /api/voice/ui-language`: the owner's UI language for the call pages (public static HTML that cannot know the user).
The answer is the same resolution the server uses for the call file's drive folder and summary."""
from unittest.mock import MagicMock

from quart import Quart

from src.domain.language import LanguageCode
from src.ports.language_service_port import LanguageServicePort
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.cabinet_session_kit import cabinet_session_for
from tests.unit.web.test_voice_web_call_app import MemoryStore, _room, _setup

AUTH: dict = {}   # the Bearer header of the app's one signed-in session, set by `_app()`


class FakeLanguages(LanguageServicePort):
    """Resolves per user from a table; a user in `broken` raises."""

    def __init__(self, table=None, broken=()):
        self.table, self.broken, self.asked = table or {}, set(broken), []

    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        self.asked.append(user_id)
        if user_id in self.broken:
            raise RuntimeError("firestore down")
        return self.table[user_id]

    async def get_preference(self, user_id):
        return None, False


def _app(languages, user="u1"):
    cabinet_sessions, _bearer = cabinet_session_for(user, "a1")
    AUTH.update(_bearer)
    store = MemoryStore()
    app = Quart(__name__)
    app.register_blueprint(create_voice_web_call_blueprint(
        cabinet_sessions=cabinet_sessions, call_setup=_setup(store), media_room=_room(), ephemeral_store=store,
        relay_base_url="wss://relay.example.com", cipher=AesGcmTestCipher(), digest_key=b"k" * 32,
        language_service=languages))
    return app


async def _get(app, headers=AUTH):
    resp = await app.test_client().get("/api/voice/ui-language", headers=headers)
    return resp, await resp.get_json()


async def test_anonymous_is_refused():
    resp, _ = await _get(_app(FakeLanguages({"u1": LanguageCode.UK})), headers={})
    assert resp.status_code == 401


async def test_the_signed_in_user_gets_the_language_resolved_for_them():
    languages = FakeLanguages({"u1": LanguageCode.UK, "u2": LanguageCode.FR})
    resp, body = await _get(_app(languages, user="u2"))
    assert resp.status_code == 200 and body == {"language": "fr"}
    assert languages.asked == ["u2"]


async def test_a_resolution_error_is_500_without_a_language():
    resp, body = await _get(_app(FakeLanguages(broken={"u1"})))
    assert resp.status_code == 500
    assert "language" not in body


async def test_no_service_wired_is_404():
    resp, body = await _get(_app(None))
    assert resp.status_code == 404
    assert "language" not in body
