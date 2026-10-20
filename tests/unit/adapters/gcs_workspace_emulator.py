"""An in-memory emulation of the google-cloud-storage surface GcsWorkspaceAdapter uses, with GCS semantics: object
versioning (an overwrite or delete makes the live version noncurrent with timeDeleted), generations, 412 on a failed
precondition, metadata merge on patch — and the trap the RFC forbids: a delete through a generation-addressed blob
ERASES that version (§5.3). The wire tests use MagicMock; this backs the store contract test."""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from google.api_core.exceptions import NotFound, PreconditionFailed


class _Version:
    def __init__(self, name, generation, data, metadata, custom_time, created):
        self.name, self.generation, self.data = name, generation, data
        self.metadata = dict(metadata)
        self.custom_time, self.time_created = custom_time, created
        self.time_deleted: Optional[datetime] = None


class FakeGcsBlob:
    """Mirrors storage.Blob: `bucket.blob(name, generation=g)` sets `generation`; upload_from_string() and patch()
    reload the properties (generation included); delete() and download_as_bytes() address `self.generation` when it is
    set — so a delete on the blob that was just patched is generation-addressed and erases that version, exactly the
    SDK behaviour the adapter must avoid."""

    def __init__(self, emulator, name, generation=None, version=None):
        self._emu, self.name = emulator, name
        self.generation = generation
        self.metadata = None
        self.custom_time = None
        self.size = self.time_created = self.time_deleted = None
        if version is not None:
            self._load(version)

    def _load(self, v):
        self.generation, self.size, self.metadata = v.generation, len(v.data), dict(v.metadata)
        self.custom_time, self.time_created, self.time_deleted = v.custom_time, v.time_created, v.time_deleted

    def upload_from_string(self, data, content_type="text/plain", if_generation_match=None, **_):
        self._load(self._emu._put(self.name, data, dict(self.metadata or {}), self.custom_time, if_generation_match))

    def patch(self, if_generation_match=None, **_):
        self._load(self._emu._patch(self.name, dict(self.metadata or {}), if_generation_match))

    def delete(self, if_generation_match=None, **_):
        self._emu._delete(self.name, self.generation, if_generation_match)

    def download_as_bytes(self, **_):
        return self._emu._find(self.name, self.generation).data


class FakeGcsBucket:
    def __init__(self, emulator):
        self._emu = emulator

    def blob(self, name, generation=None, **_):
        return FakeGcsBlob(self._emu, name, generation)

    def get_blob(self, name, generation=None, **_):
        try:
            version = self._emu._find(name, generation)
        except NotFound:
            return None
        return FakeGcsBlob(self._emu, name, version.generation, version)


class FakeGcsClient:
    def __init__(self) -> None:
        self.versions: Dict[str, List[_Version]] = {}
        self.lose_next_write_response = False   # the write lands, then the (retried) request answers 412
        self.lose_next_delete_response = False
        self._generation = 1000
        self._now = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)

    def bucket(self, name):
        return FakeGcsBucket(self)

    def list_blobs(self, bucket_name, prefix="", versions=False, **_):
        out = []
        for name in sorted(self.versions):
            if name.startswith(prefix):
                out += [FakeGcsBlob(self, name, v.generation, v)
                        for v in self.versions[name] if versions or v.time_deleted is None]
        return out

    # -- GCS semantics -------------------------------------------------------------------------------------------

    def _tick(self):
        self._now += timedelta(minutes=1)
        return self._now

    def _live(self, name):
        return next((v for v in self.versions.get(name, []) if v.time_deleted is None), None)

    def _find(self, name, generation):
        if generation is None:
            version = self._live(name)
        else:
            version = next((v for v in self.versions.get(name, []) if v.generation == generation), None)
        if version is None:
            raise NotFound(name)
        return version

    def _check(self, name, if_generation_match):
        live = self._live(name)
        if if_generation_match is not None and (live.generation if live else 0) != if_generation_match:
            raise PreconditionFailed(f"412 {name}")
        return live

    def _put(self, name, data, metadata, custom_time, if_generation_match):
        live = self._check(name, if_generation_match)
        now = self._tick()
        if live is not None:
            live.time_deleted = now
        self._generation += 1
        version = _Version(name, self._generation, data, metadata, custom_time, now)
        self.versions.setdefault(name, []).append(version)
        if self.lose_next_write_response:
            self.lose_next_write_response = False
            raise PreconditionFailed("412 on the retry of a write that landed")
        return version

    def _patch(self, name, metadata, if_generation_match):
        live = self._live(name)
        if live is None:
            raise NotFound(name)
        self._check(name, if_generation_match)
        live.metadata.update(metadata)
        return live

    def _delete(self, name, generation, if_generation_match):
        if generation is not None:  # generation-addressed: that version is gone for good
            self.versions[name] = [v for v in self.versions.get(name, []) if v.generation != generation]
            return
        live = self._live(name)
        if live is None:
            raise NotFound(name)
        self._check(name, if_generation_match)
        live.time_deleted = self._tick()
        if self.lose_next_delete_response:
            self.lose_next_delete_response = False
            raise PreconditionFailed("412 on the retry of a delete that landed")
