"""ExternalTaskFiles — the copies of the files a task carries (EXTERNAL_TASK_QUEUE_RFC §5.12, D15).

Public interface:
    ExternalTaskFiles(source, storage, *, wall_clock=time.time)
        .resolve_all(user_id, refs) -> List[ResolvedFile]       every file's original bytes, or a refusal
        .store_all(user_id, task_id, resolved) -> Tuple[TaskFile, ...]   one copy each, numbered from 1
        .link(file) -> Optional[str]                            a fresh signed GET link; None when the copy is gone
    TaskFileSource                                              the resolver's face this service reads

One module on purpose: `TaskFileSource` is the narrow face of `FileConversionService` (its `resolve_file` and
`active_kind_of`) that this service is given in place of the whole service (REQ-ARCH-22), and is read only here.

**Resolving.** Each reference goes through `resolve_file(ref, user_id, action="send")`: the original bytes, with the
caps and refusals of sending a file to the owner's chat, and an email attachment also through the active-kind check of
that send (`active_kind_of`, D13 of EXTERNAL_FILES_RFC). Nothing is parsed or converted. Every file is resolved before
any is stored, so a reference that does not resolve refuses the whole create (`TaskFileRefused`, naming it) and nothing
is written; the count is checked before any download and the running total after each one (`files_cap`,
`files_too_large`).

**Storing.** One private object per file at `external_tasks/{user_id}/{task_id}/{n}-{name}` (`task_file_key`, the name
made safe by `task_file_name`); a storage error propagates. The copies are never deleted here: the media bucket's
lifecycle rule for the prefix deletes them by age (§5.12 "Life and cleanup").

**Links.** `exists` first, so a copy the lifecycle (or a hand) removed answers None instead of a link to nothing; then a
V4 signed GET URL valid `FILE_LINK_TTL_S`, signed as the runtime identity. Errors of either call propagate (the caller
answers a retryable error).

Logs name the file by its number or its reference kind, never its name, key, reference or link.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional, Protocol, Sequence, Tuple

from ..domain.document_open import ACTION_SEND, ResolvedFile
from ..domain.exceptions import FileNotResolved
from ..domain.external_task import FILE_LINK_TTL_S, MAX_FILES_PER_TASK, TASK_FILE_CHECK_S, TASK_FILES_MAX_BYTES, TaskFile
from ..domain.external_task_files import task_file_key, task_file_name
from ..domain.external_task_refusal import ExternalTaskRefused, TaskFileRefused, TaskRefusal
from ..domain.email_open_texts import active_kind_refusal_note
from ..domain.file_ref import FileRefKind, classify_ref
from ..domain.user_drive import DriveAccessRefused, DriveRefRefused
from ..ports.media_storage_port import MediaStoragePort
from ..utils.logger import logger


class TaskFileSource(Protocol):
    async def resolve_file(self, ref: str, user_id: str, *, action: str) -> ResolvedFile: ...

    async def active_kind_of(self, resolved: ResolvedFile, *, deadline_at: float) -> Optional[str]: ...


class ExternalTaskFiles:
    def __init__(self, source: TaskFileSource, storage: MediaStoragePort, *,
                 wall_clock: Callable[[], float] = time.time) -> None:
        """`wall_clock` is the clock of the email attachment check's deadline (`active_kind_of` takes wall time)."""
        self._source = source
        self._storage = storage
        self._wall_clock = wall_clock

    async def resolve_all(self, user_id: str, refs: Sequence[str]) -> List[ResolvedFile]:
        """The original bytes of every reference, in order. `files_cap` over `MAX_FILES_PER_TASK` (nothing read);
        `files_too_large` once the bytes read pass `TASK_FILES_MAX_BYTES` (the rest is not read); `TaskFileRefused`
        for the first reference that does not resolve."""
        if len(refs) > MAX_FILES_PER_TASK:
            raise ExternalTaskRefused(TaskRefusal.FILES_CAP)
        resolved: List[ResolvedFile] = []
        total = 0
        for number, ref in enumerate(refs, start=1):
            one = await self._resolve(ref, user_id, number)
            total += len(one.data)
            if total > TASK_FILES_MAX_BYTES:
                logger.info(f"[TaskFiles] refused user={user_id[:8]} file={number}: over the total cap")
                raise ExternalTaskRefused(TaskRefusal.FILES_TOO_LARGE)
            resolved.append(one)
        return resolved

    async def _resolve(self, ref: str, user_id: str, number: int) -> ResolvedFile:
        kind = classify_ref(ref)
        try:
            resolved = await self._source.resolve_file(ref, user_id, action=ACTION_SEND)
            if kind is FileRefKind.EMAIL:
                active = await self._source.active_kind_of(
                    resolved, deadline_at=self._wall_clock() + TASK_FILE_CHECK_S)
                if active is not None:
                    raise FileNotResolved(active_kind_refusal_note(active))
        except (FileNotResolved, DriveRefRefused, DriveAccessRefused) as exc:
            logger.info(f"[TaskFiles] refused user={user_id[:8]} file={number} kind={kind.value} "
                        f"reason={type(exc).__name__}")
            raise TaskFileRefused(ref, exc.line) from exc
        except (FileNotFoundError, PermissionError) as exc:
            logger.info(f"[TaskFiles] refused user={user_id[:8]} file={number} kind={kind.value} "
                        f"reason={type(exc).__name__}")
            raise TaskFileRefused(ref, None) from exc
        except Exception as exc:
            logger.error(f"[TaskFiles] resolve failed user={user_id[:8]} file={number} kind={kind.value} "
                         f"error={type(exc).__name__}")
            raise TaskFileRefused(ref, None) from exc
        return resolved

    async def store_all(self, user_id: str, task_id: str, resolved: Sequence[ResolvedFile]) -> Tuple[TaskFile, ...]:
        """One private copy per file, numbered from 1 in the given order; the record entries in the same order."""
        files: List[TaskFile] = []
        for number, one in enumerate(resolved, start=1):
            name = task_file_name(one.name)
            key = task_file_key(user_id, task_id, number, name)
            await self._storage.store(one.data, key, one.mime)
            files.append(TaskFile(number=number, name=name, mime=one.mime, size=len(one.data), key=key))
        logger.info(f"[TaskFiles] stored user={user_id[:8]} task={task_id} files={len(files)}")
        return tuple(files)

    async def link(self, file: TaskFile) -> Optional[str]:
        """A fresh signed GET link to the copy, valid `FILE_LINK_TTL_S`; None when no copy is stored any more."""
        if not await self._storage.exists(file.key):
            return None
        return await self._storage.generate_signed_url(file.key, FILE_LINK_TTL_S)
