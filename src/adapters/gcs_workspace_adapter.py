"""
GcsWorkspaceAdapter — WorkspaceStorePort over a dedicated, versioned GCS bucket (AGENT_WORKSPACE_RFC §5).

Object name `{user_id}/{path}`; the path is validated in domain before it reaches here, so it cannot leave the
user's prefix. Every put stamps `origin`, `fire`, a fresh `write_id` (and `restored_from`), plus `customTime` under
scratch/ (the 7-day lifecycle rule counts from it). Every mutation carries a generation precondition. A delete is a
metadata patch (who deleted it, where it went, a fresh `delete_id`) and then a delete of the LIVE object, both under
`if_generation_match`, the delete through a fresh blob (after patch() the SDK blob knows its generation and would
send it): a generation-addressed delete would erase that version permanently. A 412 on a request the client retried is read back (`write_id` / `delete_id`) before it is reported as a conflict. A file-level prefix list
keeps only the exact name. Sync SDK calls run in the default executor (gcs_media_adapter precedent).
"""
import asyncio
import mimetypes
import uuid
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable, Dict, List, Optional

from google.api_core.exceptions import NotFound, PreconditionFailed

from ..domain.exceptions import WorkspaceConflict
from ..domain.workspace import (
    SCRATCH_FOLDER,
    WorkspaceBlob,
    WorkspaceDeleteMeta,
    WorkspaceEntry,
    WorkspaceWriteMeta,
)
from ..ports.workspace_store_port import WorkspaceStorePort
from ..utils.logger import logger

_TEXT_TYPES = (
    (".md", "text/markdown; charset=utf-8"),
    (".txt", "text/plain; charset=utf-8"),
    (".csv", "text/csv; charset=utf-8"),
    (".tsv", "text/tab-separated-values; charset=utf-8"),
    (".json", "application/json"),
    (".yaml", "application/yaml"),
    (".yml", "application/yaml"),
)


def _content_type(path: str) -> str:
    for extension, content_type in _TEXT_TYPES:
        if path.lower().endswith(extension):
            return content_type
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _new_id() -> str:
    return uuid.uuid4().hex


class GcsWorkspaceAdapter(WorkspaceStorePort):
    """A put stamps `customTime` under scratch/ (the bucket's 7-day lifecycle rule counts from it). A delete is two
    requests: a metadata patch of the live version, then a delete of the live object through a fresh blob, both under
    `if_generation_match` (a generation-addressed delete would erase the version)."""

    def __init__(self, bucket_name: str, client: Optional[Any] = None, new_id: Callable[[], str] = _new_id) -> None:
        self._bucket_name = bucket_name
        self._client = client  # injected by tests (the SDK boundary); built lazily on first use otherwise
        self._new_id = new_id

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import storage  # type: ignore[attr-defined]  # lazy import — optional at startup; mypy resolves google.cloud to a stub namespace without it
            self._client = storage.Client()
        return self._client

    def _bucket(self) -> Any:
        return self._get_client().bucket(self._bucket_name)

    @staticmethod
    def _key(user_id: str, path: str) -> str:
        return f"{user_id}/{path}"

    @staticmethod
    async def _off_loop(fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, partial(fn, *args))

    # -- port --------------------------------------------------------------------------------------------------

    async def list(self, user_id: str, prefix: str, include_versions: bool = False) -> List[WorkspaceEntry]:
        return await self._off_loop(self._list_sync, user_id, prefix, include_versions)

    async def read(self, user_id: str, path: str, generation: Optional[int] = None) -> WorkspaceBlob:
        return await self._off_loop(self._read_sync, user_id, path, generation)

    async def put(self, user_id: str, path: str, data: bytes, meta: WorkspaceWriteMeta,
                  if_generation_match: int) -> WorkspaceEntry:
        return await self._off_loop(self._put_sync, user_id, path, data, meta, if_generation_match)

    async def delete(self, user_id: str, path: str, if_generation_match: int, meta: WorkspaceDeleteMeta) -> None:
        await self._off_loop(self._delete_sync, user_id, path, if_generation_match, meta)

    # -- sync bodies (executor) --------------------------------------------------------------------------------

    def _list_sync(self, user_id: str, prefix: str, include_versions: bool) -> List[WorkspaceEntry]:
        key = self._key(user_id, prefix)
        exact = bool(prefix) and not prefix.endswith("/")
        blobs = self._get_client().list_blobs(self._bucket_name, prefix=key, versions=include_versions)
        return [self._entry(user_id, b) for b in blobs if not exact or b.name == key]

    def _read_sync(self, user_id: str, path: str, generation: Optional[int]) -> WorkspaceBlob:
        key = self._key(user_id, path)
        bucket = self._bucket()
        blob = bucket.get_blob(key, generation=generation)  # GET 1: metadata
        if blob is None:
            raise FileNotFoundError(path)
        try:
            data = bucket.blob(key, generation=blob.generation).download_as_bytes()  # GET 2: media, that version
        except NotFound as e:
            raise FileNotFoundError(path) from e
        return WorkspaceBlob(data=data, entry=self._entry(user_id, blob))

    def _put_sync(self, user_id: str, path: str, data: bytes, meta: WorkspaceWriteMeta,
                  if_generation_match: int) -> WorkspaceEntry:
        key = self._key(user_id, path)
        bucket = self._bucket()
        write_id = self._new_id()
        metadata: Dict[str, str] = {"origin": meta.origin, "write_id": write_id}
        if meta.fire:
            metadata["fire"] = meta.fire
        if meta.restored_from is not None:
            metadata["restored_from"] = str(meta.restored_from)
        blob = bucket.blob(key)
        blob.metadata = metadata
        if path.startswith(SCRATCH_FOLDER):
            blob.custom_time = datetime.now(timezone.utc)  # R1: the 7-day rule counts from the last write
        try:
            blob.upload_from_string(data, content_type=_content_type(path), if_generation_match=if_generation_match)
        except PreconditionFailed as e:
            # The client retries a request whose response was lost; the retry meets its own write and gets 412.
            live = bucket.get_blob(key)
            if live is not None and (live.metadata or {}).get("write_id") == write_id:
                logger.info("[Workspace] 412 on a retried write that had landed: %s", path)
                return self._entry(user_id, live)
            raise WorkspaceConflict(path) from e
        return self._entry(user_id, blob)

    def _delete_sync(self, user_id: str, path: str, if_generation_match: int, meta: WorkspaceDeleteMeta) -> None:
        key = self._key(user_id, path)
        bucket = self._bucket()
        delete_id = self._new_id()
        # All four keys, always: patch() merges, and a None removes the key, so a failed earlier delete cannot leave
        # a stale deleted_fire / renamed_to on the live version.
        metadata: Dict[str, Optional[str]] = {
            "deleted_by": meta.deleted_by,
            "delete_id": delete_id,
            "deleted_fire": meta.deleted_fire or None,
            "renamed_to": meta.renamed_to or None,
        }
        # The LIVE object. A delete must never carry a generation: in a versioned bucket that erases the version.
        blob = bucket.blob(key)
        blob.metadata = metadata
        try:
            blob.patch(if_generation_match=if_generation_match)  # bumps the metageneration only
            # A FRESH blob: patch() reloaded blob.generation and Blob.delete() sends generation=self.generation —
            # that would erase the version (google-cloud-storage 3.9.0, blob.py ~851-861).
            bucket.blob(key).delete(if_generation_match=if_generation_match)
        except (PreconditionFailed, NotFound) as e:
            if self._delete_landed(key, delete_id):
                logger.info("[Workspace] %s on a retried delete that had landed: %s", type(e).__name__, path)
                return
            if isinstance(e, NotFound):
                raise FileNotFoundError(path) from e
            raise WorkspaceConflict(path) from e

    def _delete_landed(self, key: str, delete_id: str) -> bool:
        """No live object, and the newest noncurrent version carries our delete_id (§5.3)."""
        if self._bucket().get_blob(key) is not None:
            return False
        versions = [
            b for b in self._get_client().list_blobs(self._bucket_name, prefix=key, versions=True) if b.name == key
        ]
        if not versions:
            return False
        newest = max(versions, key=lambda b: int(b.generation))
        return bool((newest.metadata or {}).get("delete_id") == delete_id)

    @staticmethod
    def _entry(user_id: str, blob: Any) -> WorkspaceEntry:
        meta: Dict[str, str] = dict(blob.metadata or {})
        restored = str(meta.get("restored_from") or "")
        return WorkspaceEntry(
            path=str(blob.name)[len(user_id) + 1:],
            generation=int(blob.generation),
            size=int(blob.size or 0),
            updated=blob.time_created or datetime.now(timezone.utc),
            origin=meta.get("origin", ""),
            fire=meta.get("fire"),
            write_id=meta.get("write_id"),
            live=blob.time_deleted is None,
            deleted_at=blob.time_deleted,
            deleted_by=meta.get("deleted_by"),
            deleted_fire=meta.get("deleted_fire"),
            delete_id=meta.get("delete_id"),
            renamed_to=meta.get("renamed_to"),
            restored_from=int(restored) if restored.isdigit() else None,
        )
