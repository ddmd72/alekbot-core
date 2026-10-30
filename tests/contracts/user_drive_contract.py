"""The drive contract suite (MULTI_DRIVE_RFC §13 T-3): one set of cases every `UserDrivePort` implementation passes.

A backend subclasses `UserDriveContract` in its own test module, defines a `world` fixture returning a `DriveWorld`, and
inherits every case. The cases use the port for everything the port can do; the world supplies only what the provider
side must do by itself (plant a typed facet, close an item to id reads, expire a cursor, look into the recycle bin,
say whether the adapter still holds state for a connection).

Cases: identities in the adapter's `id_space`; content version and content identity; the three conflict modes;
404 is not deletion; resync; recoverable delete; typed media facts only (`alias_of` included); every name of the portable
set accepted; a `MediaSource` the sandbox may receive (no headers) or bytes that stream; capabilities (T-7: one case per
value the world declares); the path limit; two handles of one adapter share no cache, and `forget` drops one's own.
A drive that is read-only by design (the eval fixture) declares `writable = False` and skips the cases that write beyond
`upload`; a drive that cannot isolate connections declares `isolates_connections = False`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List, Tuple

import pytest

from src.domain.drive_handle import DriveHandle
from src.domain.drive_index import DriveChange
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.drive_media_source import MediaSource
from src.domain.user_drive import (
    DriveFileTooLarge, DriveItemNotFoundError, DriveNameConflictError, DrivePathTooLong, DriveResyncRequired,
)
from src.ports.user_drive_port import UserDrivePort

PORTABLE_NAMES = ("report.pdf", "a b.txt", "Відомість №1.md", "100% done.txt", "x&y+z.txt", "o'clock.txt",
                  "#tag.md", "ünï.txt", "a,b;c.txt", "(1) copy.txt", "[draft] v2.txt", "~tmp.txt")
MEDIA_KINDS = ("photo", "video", "audio", "location", "package", "malware", "alias")


class DriveWorld(ABC):
    """What a backend gives the suite."""

    port: UserDrivePort
    handle: DriveHandle
    other: DriveHandle                       # a second connection of the same adapter
    id_space: str
    max_path_chars: int
    writable: bool = True
    isolates_connections: bool = True
    enforces_path_limit: bool = True

    @abstractmethod
    async def plant_file_without_hash(self, handle: DriveHandle, parent_id: str, name: str) -> str: ...

    @abstractmethod
    async def plant_media(self, handle: DriveHandle, kind: str) -> str:
        """A file whose provider facets are the fixed values the cases expect for `kind` (MEDIA_KINDS)."""

    @abstractmethod
    async def plant_closed(self, handle: DriveHandle, parent_id: str, name: str) -> str:
        """A file the change feed lists and every id read answers "not found" to (USER_DRIVE_INDEXING_RFC §26.2)."""

    @abstractmethod
    async def plant_thumbnail(self, handle: DriveHandle, parent_id: str, name: str, data: bytes) -> str: ...

    @abstractmethod
    def expired(self, handle: DriveHandle, cursor: str) -> str:
        """A cursor the provider no longer honours (made from `cursor`)."""

    @abstractmethod
    def in_recycle_bin(self, handle: DriveHandle, item_id: str) -> bool: ...

    @abstractmethod
    def holds_state_for(self, handle: DriveHandle) -> bool:
        """True while the adapter holds any cache, lock or token for the connection."""


async def drain(port: UserDrivePort, handle: DriveHandle, cursor: "str | None" = None) -> Tuple[List[DriveChange], str]:
    """Every change of one round (pages followed) and the cursor it ends with."""
    out: List[DriveChange] = []
    while True:
        page = await port.get_changes(handle, cursor)
        out.extend(page.changes)
        cursor = page.cursor
        if page.complete:
            return out, cursor


async def _root(world: DriveWorld, handle: "DriveHandle | None" = None) -> str:
    return (await world.port.get_root(handle or world.handle)).item_id


def _need_write(world: DriveWorld) -> None:
    if not world.writable:
        pytest.skip("a read-only drive by design")


class UserDriveContract:
    # -- identities and content -----------------------------------------------------------------------------

    async def test_identities_are_adapter_built_in_the_id_space(self, world: DriveWorld) -> None:
        port, h = world.port, world.handle
        root = await port.get_root(h)
        item = await port.upload(h, root.item_id, "a.txt", b"hello", "text/plain")
        prefix = world.id_space + ":"
        assert root.is_folder and root.path == ""
        assert item.drive_identity.startswith(prefix) and item.identity.startswith(item.drive_identity + ":")
        assert item.item_id in item.identity
        changes, _ = await drain(port, h)
        listed = [c for c in changes if c.item_id == item.item_id]
        assert listed and listed[0].identity == item.identity
        assert all(c.identity.startswith(prefix) for c in changes)
        assert len({c.identity for c in changes}) == len(changes)
        round_ = await port.get_changes(h, None)
        assert round_.drive_identity == item.drive_identity

    async def test_content_version_changes_with_content_only(self, world: DriveWorld) -> None:
        _need_write(world)
        port, h = world.port, world.handle
        item = await port.upload(h, await _root(world), "v.txt", b"one", "text/plain")
        again = await port.get_item(h, item.item_id)
        assert item.content_version and again.content_version == item.content_version
        edited = await port.replace_content(h, item.item_id, b"two!", "text/plain")
        assert edited.item_id == item.item_id and edited.content_version != item.content_version
        assert (await port.download(h, item.item_id)) == b"two!"
        folder = await port.create_folder(h, await _root(world), "dir")
        assert folder.content_version == "" and folder.content_identity is None

    async def test_content_identity_is_family_digest_size_or_none(self, world: DriveWorld) -> None:
        port, h = world.port, world.handle
        data = b"0123456789"
        item = await port.upload(h, await _root(world), "hashed.bin", data, "application/octet-stream")
        ci = item.content_identity
        assert ci is not None and ci.size == len(data) and ci.digest
        assert ci.render() == f"{ci.family}:{ci.digest}:{ci.size}"
        bare = await world.plant_file_without_hash(h, await _root(world), "bare.bin")
        assert (await port.get_change(h, bare)).content_identity is None
        assert (await port.get_item(h, bare)).content_identity is None

    # -- conflicts and writes -------------------------------------------------------------------------------

    async def test_conflict_modes(self, world: DriveWorld) -> None:
        port, h = world.port, world.handle
        root = await _root(world)
        first = await port.upload(h, root, "c.md", b"1", "text/markdown")
        with pytest.raises(DriveNameConflictError):
            await port.upload(h, root, "c.md", b"2", "text/markdown", conflict="fail")
        renamed = await port.upload(h, root, "c.md", b"3", "text/markdown", conflict="rename")
        assert renamed.item_id != first.item_id and renamed.name != "c.md"
        assert (await port.download(h, first.item_id)) == b"1"
        replaced = await port.upload(h, root, "c.md", b"4", "text/markdown", conflict="replace")
        assert replaced.item_id == first.item_id and (await port.download(h, first.item_id)) == b"4"
        assert [c.name for c in (await port.list_children(h, root)) if c.name == "c.md"] == ["c.md"]

    async def test_move_and_create_folder(self, world: DriveWorld) -> None:
        _need_write(world)
        port, h = world.port, world.handle
        root = await _root(world)
        folder = await port.create_folder(h, root, "docs")
        with pytest.raises(DriveNameConflictError):
            await port.create_folder(h, root, "docs")
        a = await port.upload(h, root, "a.txt", b"a", "text/plain")
        b = await port.upload(h, root, "b.txt", b"b", "text/plain")
        moved = await port.move(h, a.item_id, new_parent_id=folder.item_id, new_name="z.txt")
        assert moved.item_id == a.item_id and moved.path == "docs/z.txt"
        with pytest.raises(DriveNameConflictError):
            await port.move(h, b.item_id, new_name="docs")
        found = await port.child_by_name(h, folder.item_id, "z.txt")
        assert found is not None and found.item_id == a.item_id
        assert await port.child_by_name(h, folder.item_id, "nope.txt") is None

    async def test_delete_is_recoverable_and_the_feed_says_deleted(self, world: DriveWorld) -> None:
        _need_write(world)
        port, h = world.port, world.handle
        item = await port.upload(h, await _root(world), "gone.txt", b"x", "text/plain")
        _, cursor = await drain(port, h)
        await port.delete(h, item.item_id)
        assert world.in_recycle_bin(h, item.item_id)
        with pytest.raises(DriveItemNotFoundError):
            await port.get_item(h, item.item_id)
        round_, _ = await drain(port, h, cursor)
        assert [c.item_id for c in round_ if c.deleted] == [item.item_id]

    async def test_portable_names_are_accepted_and_read_back_exactly(self, world: DriveWorld) -> None:
        port, h = world.port, world.handle
        root = await _root(world)
        for name in PORTABLE_NAMES:
            item = await port.upload(h, root, name, b"x", "text/plain", conflict="fail")
            assert item.name == name and item.path == name
            found = await port.child_by_name(h, root, name)
            assert found is not None and found.item_id == item.item_id and found.name == name
        names = {c.name for c in await port.list_children(h, root)}
        assert set(PORTABLE_NAMES) <= names

    async def test_path_too_long_is_refused(self, world: DriveWorld) -> None:
        if not world.enforces_path_limit:
            pytest.skip("this drive does not enforce a path limit")
        port, h = world.port, world.handle
        with pytest.raises(DrivePathTooLong):
            await port.upload(h, await _root(world), "n" * world.max_path_chars, b"x", "text/plain")

    # -- reads ----------------------------------------------------------------------------------------------

    async def test_not_found_is_an_error_never_a_deletion(self, world: DriveWorld) -> None:
        port, h = world.port, world.handle
        root = await _root(world)
        for call in (port.get_item(h, "missing-1"), port.get_change(h, "missing-1"), port.download(h, "missing-1")):
            with pytest.raises(DriveItemNotFoundError):
                await call
        closed = await world.plant_closed(h, root, "closed.bin")
        listed, cursor = await drain(port, h)
        assert closed in {c.item_id for c in listed if not c.deleted}
        with pytest.raises(DriveItemNotFoundError):
            await port.get_change(h, closed)
        with pytest.raises(DriveItemNotFoundError):
            await port.get_item(h, closed)
        again, _ = await drain(port, h, cursor)
        assert closed not in {c.item_id for c in again if c.deleted}

    async def test_the_feed_enumerates_then_rounds_and_resyncs(self, world: DriveWorld) -> None:
        port, h = world.port, world.handle
        item = await port.upload(h, await _root(world), "f.txt", b"f", "text/plain")
        first = await port.get_changes(h, None)
        assert first.full_enumeration and first.drive_identity
        changes, cursor = await drain(port, h)
        assert item.item_id in {c.item_id for c in changes}
        quiet = await port.get_changes(h, cursor)
        assert not quiet.full_enumeration and quiet.complete
        assert [c.item_id for c in quiet.changes if c.item_id == item.item_id] == []
        with pytest.raises(DriveResyncRequired):
            await port.get_changes(h, world.expired(h, cursor))

    async def test_children_pages_cover_the_folder_once(self, world: DriveWorld) -> None:
        port, h = world.port, world.handle
        root = await _root(world)
        folder = (await port.create_folder(h, root, "many")).item_id if world.writable else root
        before = {c.item_id for c in (await port.list_children_changes(h, folder, 1000)).changes}
        for i in range(5):
            await port.upload(h, folder, f"p{i}.txt", b"p", "text/plain")
        seen: List[str] = []
        cursor = None
        while True:
            page = await port.list_children_changes(h, folder, 2, cursor)
            assert len(page.changes) <= 2
            seen.extend(c.item_id for c in page.changes)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == len(before) + 5

    async def test_search_respects_the_limit(self, world: DriveWorld) -> None:
        port, h = world.port, world.handle
        try:
            for i in range(3):
                await port.upload(h, await _root(world), f"needle-{i}.txt", b"n", "text/plain")
            hits = await port.search(h, "needle", 2)
        except NotImplementedError:
            pytest.skip("a drive without search")
        assert len(hits) == 2 and all("needle" in x.name for x in hits)

    # -- typed facts ----------------------------------------------------------------------------------------

    @pytest.mark.parametrize("kind", MEDIA_KINDS)
    async def test_media_facts_are_typed(self, world: DriveWorld, kind: str) -> None:
        port, h = world.port, world.handle
        item_id = await world.plant_media(h, kind)
        for media in ((await port.get_change(h, item_id)).media, (await port.get_item(h, item_id)).media):
            assert isinstance(media, DriveMediaFacts)
            if kind == "photo":
                assert media.taken_at == datetime(2025, 5, 1, 10, 0, tzinfo=timezone.utc)
                assert (media.camera_make, media.camera_model) == ("Acme", "C1")
            elif kind == "video":
                assert (media.video_duration_ms, media.video_width, media.video_height) == (61000, 1920, 1080)
            elif kind == "audio":
                assert media.audio_duration_ms == 30000
            elif kind == "location":
                assert media.location == (41.15, -8.61)
            elif kind == "package":
                assert media.package is True
            elif kind == "malware":
                assert media.malware is True
            else:
                assert media.is_alias and isinstance(media.alias_of, str)
        change = await port.get_change(h, item_id)
        assert not change.facets                       # the raw provider mapping is gone

    async def test_an_ordinary_file_has_neutral_media(self, world: DriveWorld) -> None:
        port, h = world.port, world.handle
        item = await port.upload(h, await _root(world), "plain.txt", b"p", "text/plain")
        change = await port.get_change(h, item.item_id)
        assert change.media == DriveMediaFacts() and change.media.alias_of is None
        assert item.media == DriveMediaFacts()

    # -- content --------------------------------------------------------------------------------------------

    async def test_media_source_is_header_free_or_the_bytes_stream(self, world: DriveWorld, tmp_path) -> None:
        port, h = world.port, world.handle
        item = await port.upload(h, await _root(world), "m.bin", b"stream-me", "application/octet-stream")
        source = await port.media_source(h, item.item_id)
        assert isinstance(source, MediaSource) and source.url not in repr(source)
        dest = tmp_path / "out"
        if not source.header_free:                      # the sandbox gets bytes, never a token
            assert await port.download_to_file(h, item.item_id, str(dest), 1024) == len(b"stream-me")
            assert dest.read_bytes() == b"stream-me"
        with pytest.raises(DriveItemNotFoundError):
            await port.media_source(h, "missing-1")
        with pytest.raises(DriveItemNotFoundError):
            await port.media_source(h, await _root(world))

    async def test_download_to_file_streams_and_stops_at_the_cap(self, world: DriveWorld, tmp_path) -> None:
        port, h = world.port, world.handle
        item = await port.upload(h, await _root(world), "big.bin", b"x" * 100, "application/octet-stream")
        assert await port.download_to_file(h, item.item_id, str(tmp_path / "ok"), 100) == 100
        with pytest.raises(DriveFileTooLarge):
            await port.download_to_file(h, item.item_id, str(tmp_path / "cut"), 10)
        with pytest.raises(DriveItemNotFoundError):
            await port.download_to_file(h, "missing-1", str(tmp_path / "none"), 10)

    async def test_thumbnail_is_bytes_none_or_a_refusal_of_a_bad_edge(self, world: DriveWorld) -> None:
        port, h = world.port, world.handle
        root = await _root(world)
        text = await port.upload(h, root, "t.txt", b"t", "text/plain")
        assert await port.get_thumbnail(h, text.item_id, 800) is None
        pic = await world.plant_thumbnail(h, root, "pic.jpg", b"\xff\xd8thumb")
        assert await port.get_thumbnail(h, pic, 800) == b"\xff\xd8thumb"
        with pytest.raises(ValueError):
            await port.get_thumbnail(h, pic, 0)
        with pytest.raises(DriveItemNotFoundError):
            await port.get_thumbnail(h, "missing-1", 800)

    # -- per connection -------------------------------------------------------------------------------------

    async def test_capabilities_state_the_path_limit(self, world: DriveWorld) -> None:
        caps = await world.port.capabilities(world.handle)
        assert caps.max_path_chars == world.max_path_chars

    async def test_area_path_is_absolute(self, world: DriveWorld) -> None:
        assert (await world.port.area_path(world.handle)).startswith("/")

    async def test_two_handles_of_one_adapter_share_no_cache(self, world: DriveWorld) -> None:
        if not world.isolates_connections:
            pytest.skip("one drive serves every handle")
        port, a, b = world.port, world.handle, world.other
        root_a, root_b = await port.get_root(a), await port.get_root(b)
        assert root_a.drive_identity != root_b.drive_identity
        item_a = await port.upload(a, root_a.item_id, "only-a.txt", b"a", "text/plain")
        item_b = await port.upload(b, root_b.item_id, "only-b.txt", b"b", "text/plain")
        assert item_a.identity != item_b.identity
        assert {c.name for c in await port.list_children(a, root_a.item_id)} == {"only-a.txt"}
        assert {c.name for c in await port.list_children(b, root_b.item_id)} == {"only-b.txt"}
        assert (await port.get_root(a)).drive_identity == root_a.drive_identity     # a second read, still its own
        assert world.holds_state_for(a) and world.holds_state_for(b)

    async def test_forget_drops_one_connections_state_only(self, world: DriveWorld) -> None:
        if not world.isolates_connections:
            pytest.skip("one drive serves every handle")
        port, a, b = world.port, world.handle, world.other
        await port.get_root(a)
        await port.get_root(b)
        await port.forget(a)
        assert not world.holds_state_for(a) and world.holds_state_for(b)
        await port.forget(a)                             # twice is harmless
        assert (await port.get_root(a)).is_folder        # the drive is still there; the next call reads afresh
        assert world.holds_state_for(a)
