"""Wire — thumbnails and the media source (GOOGLE_DRIVE_RFC §3.2 rows `get_thumbnail` and `media_source`, GE-17, G2).
A thumbnail is fetched from its `thumbnailLink` with the `=s<n>` suffix set to the asked edge, without the bearer, https
only; the link is a secret, absent from logs and errors on every failure. The media source is the `alt=media` URL with
the bearer as a header and an expiry at least 10 minutes away (a token living less is refreshed first); url and headers
never reach a repr or a log. Served by the stateful Drive fake."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.google.google_drive_adapter import API, MEDIA_TOKEN_MIN_LIFE, THUMBNAIL_MAX_BYTES
from src.adapters.google.google_drive_mapping import sized_thumbnail
from src.domain.user_drive import DriveItemNotFoundError, DriveThumbnailTooLarge
from tests.support.google_drive_fake import THUMB, google_setup


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


def _photo(setup, thumbnail=b"\xff\xd8thumb", **kw):
    return setup.a.add_file(setup.a.root.id, "garden.jpg", b"img", mime="image/jpeg", thumbnail=thumbnail, **kw)


async def test_the_thumbnail_is_fetched_resized_and_without_the_bearer(setup):
    node = _photo(setup)
    assert await setup.adapter.get_thumbnail(setup.handle, node.id, 800) == b"\xff\xd8thumb"
    fetch = setup.fake.calls("thumbnail")
    assert len(fetch) == 1 and fetch[0].url == f"{THUMB}/{node.id}=s800"
    assert "Authorization" not in fetch[0].headers


def test_the_size_suffix_is_set_or_appended():
    assert sized_thumbnail("https://thumb.example.invalid/x=s220", 1600) == "https://thumb.example.invalid/x=s1600"
    assert sized_thumbnail("https://thumb.example.invalid/x", 64) == "https://thumb.example.invalid/x=s64"


async def test_a_link_that_is_not_https_is_no_thumbnail(setup, caplog):
    node = _photo(setup, thumbnail_link="http://thumb.example.invalid/secret-link=s220")
    with caplog.at_level(logging.DEBUG):
        assert await setup.adapter.get_thumbnail(setup.handle, node.id, 800) is None
    assert setup.fake.calls("thumbnail") == [] and "secret-link" not in caplog.text


async def test_no_link_is_none(setup):
    node = setup.a.add_file(setup.a.root.id, "brief.docx", b"b")
    assert await setup.adapter.get_thumbnail(setup.handle, node.id, 800) is None


async def test_a_gone_rendition_is_none(setup):
    node = _photo(setup)
    setup.fake.fail("thumbnail", 404)
    assert await setup.adapter.get_thumbnail(setup.handle, node.id, 800) is None


@pytest.mark.parametrize("failure,error", [(403, ValueError), (500, ValueError), ("timeout", asyncio.TimeoutError),
                                           ("transport", OSError)])
async def test_a_failed_fetch_never_shows_the_link(setup, caplog, failure, error):
    node = _photo(setup)
    setup.fake.fail("thumbnail", failure)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(error) as caught:
            await setup.adapter.get_thumbnail(setup.handle, node.id, 800)
    assert THUMB not in caplog.text and "thumb.example" not in str(caught.value)
    assert "=s800" not in caplog.text


async def test_a_rendition_over_the_vision_cap_is_too_large(setup):
    node = _photo(setup, thumbnail=b"x" * (THUMBNAIL_MAX_BYTES + 1))
    with pytest.raises(DriveThumbnailTooLarge) as caught:
        await setup.adapter.get_thumbnail(setup.handle, node.id, 800)
    assert caught.value.limit_bytes == THUMBNAIL_MAX_BYTES and caught.value.size == "s800"


async def test_the_media_source_is_the_alt_media_url_with_the_bearer_header(setup, caplog):
    node = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk", mime="video/mp4")
    with caplog.at_level(logging.DEBUG):
        source = await setup.adapter.media_source(setup.handle, node.id)
    assert source.url.startswith(f"{API}/files/{node.id}?") and "alt=media" in source.url
    assert "supportsAllDrives=true" in source.url
    assert dict(source.headers) == {"Authorization": "Bearer tok-a"} and not source.header_free
    assert source.expires_at >= datetime.now(timezone.utc) + MEDIA_TOKEN_MIN_LIFE
    assert source.url not in repr(source) and "tok-a" not in repr(source)
    assert node.id + "?" not in caplog.text and "tok-a" not in caplog.text
    assert setup.fake.calls("media") == []                   # nothing is fetched here


async def test_a_token_living_less_than_ten_minutes_is_refreshed_first(monkeypatch, caplog):
    setup = await google_setup(monkeypatch, token_expiry=datetime.now(timezone.utc) + timedelta(minutes=7))
    node = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk", mime="video/mp4")
    with caplog.at_level(logging.DEBUG):
        source = await setup.adapter.media_source(setup.handle, node.id)
    assert len(setup.fake.calls("token")) == 1
    token = dict(source.headers)["Authorization"].removeprefix("Bearer ")
    assert token.startswith("tok-perm-a-") and token not in caplog.text
    assert source.expires_at >= datetime.now(timezone.utc) + MEDIA_TOKEN_MIN_LIFE
    stored = await setup.store.repo.get(setup.handle.user_id, setup.handle.credential_id)
    assert stored.access_token == token


async def test_a_folder_or_a_trashed_item_has_no_media_source(setup):
    folder = setup.a.add_folder(setup.a.root.id, "Projects")
    talk = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk")
    setup.a.trash(talk)
    for item_id in (folder.id, talk.id, "fid-missing"):
        with pytest.raises(DriveItemNotFoundError):
            await setup.adapter.media_source(setup.handle, item_id)


async def test_n7_the_thumbnail_fetch_does_not_follow_redirects(setup, monkeypatch):
    from tests.support import google_drive_fake as gdf
    node = _photo(setup)
    seen = []
    original = gdf._Session.get

    def spy(self, url, **kw):
        if url.startswith(THUMB):
            seen.append(kw.get("allow_redirects"))
        return original(self, url, **kw)

    monkeypatch.setattr(gdf._Session, "get", spy)
    assert await setup.adapter.get_thumbnail(setup.handle, node.id, 800) == b"\xff\xd8thumb"
    assert seen == [False]


async def test_n7_a_redirect_answer_is_no_rendition(setup):
    node = _photo(setup)
    setup.fake.fail("thumbnail", 302)
    assert await setup.adapter.get_thumbnail(setup.handle, node.id, 800) is None


@pytest.mark.parametrize("failure,error", [("timeout", asyncio.TimeoutError), ("transport", OSError)])
async def test_n7_the_link_never_sits_in_an_exception_chain(setup, failure, error):
    node = _photo(setup)
    setup.fake.fail("thumbnail", failure)
    with pytest.raises(error) as caught:
        await setup.adapter.get_thumbnail(setup.handle, node.id, 800)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
