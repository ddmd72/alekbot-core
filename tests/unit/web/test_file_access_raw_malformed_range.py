"""/f/<token>/raw with a malformed Range header serves the whole file as one range instead of failing (P7, C1:
`src/web/file_access_app.py:137-140`). Real token service and session service; the media store is in memory."""
from quart import Quart

from src.services.file_access_token_service import FileAccessTokenService
from src.web.file_access_app import create_file_access_blueprint
from tests.unit.web.gate_fakes import SESSION_SECRET, session_service


class Media:
    def __init__(self, files: dict) -> None:
        self.files = files

    async def fetch(self, key: str) -> bytes:
        return self.files[key]


async def test_a_non_numeric_range_falls_back_to_the_whole_file():
    tokens = FileAccessTokenService(SESSION_SECRET)
    app = Quart("file_access_c1")
    app.register_blueprint(create_file_access_blueprint(
        token_service=tokens, media_storage=Media({"videos/u1/clip.mp4": b"0123456789"}),
        session_service=session_service()))
    token = tokens.mint(key="videos/u1/clip.mp4", user_id="u1")
    resp = await app.test_client().get(f"/f/{token}/raw", headers={"Range": "bytes=start-end"})
    assert resp.status_code == 206
    assert await resp.get_data() == b"0123456789"
    assert resp.headers["Content-Range"] == "bytes 0-9/10"
