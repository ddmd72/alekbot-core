"""WorkspaceService — the eight commands of Smart's `workspace` tool (docs/10_rfcs/AGENT_WORKSPACE_RFC.md §6–§7).

Stateless: the per-execution session (generations seen, the lock, the shield) lives in the tool handler
(`infrastructure/workspace_tools.py`), which passes what it knows in a WorkspaceCall and applies the generations the
result reports. `run` never raises for a rule, a conflict, a missing file or a store failure: each becomes a
`SYSTEM:` result (§7.5). Every command writes exactly one `[Workspace]` line (§16) — a failed mutation at WARNING, a
store error at ERROR, everything else at INFO. Content is never logged.
"""

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional

from ..domain import drive_ref
from ..domain.exceptions import WorkspaceConflict, WorkspaceRefused
from ..domain.workspace import (
    COMMANDS,
    COPY_FOLDER,
    FOLDERS,
    MUTATING_COMMANDS,
    TEXT_EXTENSIONS,
    TEXT_FOLDERS,
    WORKSPACE_REF_PREFIX,
    WorkspaceBlob,
    WorkspaceCall,
    WorkspaceDeleteMeta,
    WorkspaceResult,
    WorkspaceWriteMeta,
    apply_str_replace,
    binary_message,
    changed_lines,
    check_caps,
    check_replace_caps,
    check_file_size,
    find_references,
    folder_of,
    format_size,
    is_text,
    number_lines,
    parse_generation,
    parse_workspace_path,
    prior_generation_before_fire,
    render_history,
    render_listing,
    render_missing,
    render_same_fire_line,
    render_view_header,
    window_note,
    workspace_kind,
)
from ..ports.workspace_store_port import WorkspaceStorePort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .file_conversion_service import FileConversionService

_UNAVAILABLE = "SYSTEM: the workspace is unavailable right now; nothing was changed."
_CONFLICT = (
    "SYSTEM: {path} changed since you read it (another conversation or run wrote it). "
    "View it again and redo your change."
)


@dataclass
class _LogFields:
    path: str
    result: str = "ok"
    had_history: str = "-"
    nbytes: int = 0
    gen: str = "-"


def _decode(data: bytes) -> Optional[str]:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _lines(text: str) -> str:
    n = len(text.splitlines())
    return f"{n} line{'' if n == 1 else 's'}"


class WorkspaceService:

    def __init__(self, store: WorkspaceStorePort, file_conversion: Optional["FileConversionService"] = None) -> None:
        self._store = store
        # Copy sources from chat uploads and delivered documents (§7.3 copy). None → such copies are refused.
        self._file_conversion = file_conversion

    async def run(self, call: WorkspaceCall, command: str, args: Mapping[str, Any]) -> WorkspaceResult:
        started = time.monotonic()
        log = _LogFields(path=str(args.get("path") or ""))
        try:
            result = await self._dispatch(call, command, args, log)
        except WorkspaceRefused as e:
            log.result = f"refused:{e.reason}"
            result = WorkspaceResult(text=f"SYSTEM: {e}.", failed=True)
        except WorkspaceConflict:
            log.result = "conflict"
            result = WorkspaceResult(text=_CONFLICT.format(path=log.path), failed=True)
        except FileNotFoundError:
            log.result = "not_found"
            result = WorkspaceResult(
                text=f"SYSTEM: {log.path} does not exist; view it to see its history.", failed=True,
            )
        except Exception as e:
            log.result = "error"
            logger.error("❌ [Workspace] store failure op=%s user=%s: %s", command, call.user_id[:8], e,
                         exc_info=True)
            result = WorkspaceResult(text=_UNAVAILABLE, failed=True)
        self._log(command, call, log, started)
        return result

    async def _dispatch(
        self, call: WorkspaceCall, command: str, args: Mapping[str, Any], log: _LogFields,
    ) -> WorkspaceResult:
        if command == "view":
            return await self._view(call, args, log)
        if command == "history":
            return await self._history(call, args, log)
        if command == "create":
            return await self._create(call, args, log)
        if command == "str_replace":
            return await self._str_replace(call, args, log)
        if command == "delete":
            return await self._delete(call, args, log)
        if command == "rename":
            return await self._rename(call, args, log)
        if command == "restore":
            return await self._restore(call, args, log)
        if command == "copy":
            return await self._copy(call, args, log)
        raise WorkspaceRefused(f"unknown command '{command}'; use one of: {', '.join(COMMANDS)}", reason="command")

    @staticmethod
    def _log(command: str, call: WorkspaceCall, f: _LogFields, started: float) -> None:
        if f.result == "error":
            emit = logger.error
        elif f.result != "ok" and command in MUTATING_COMMANDS:
            emit = logger.warning
        else:
            emit = logger.info
        # §16 form: one token per field (a raw path may hold spaces or newlines; capped), fire=<note_id>@<due_at>.
        emit(
            "[Workspace] op=%s user=%s path=%s origin=%s fire=%s had_history=%s bytes=%d gen=%s result=%s ms=%d",
            command, call.user_id[:8], "_".join(f.path.split())[:120] or '""', call.origin,
            (call.fire_key or "-").removeprefix("reminder:"), f.had_history, f.nbytes, f.gen, f.result,
            int((time.monotonic() - started) * 1000),
        )

    # -- view / history ----------------------------------------------------------------------------------------

    async def _view(self, call: WorkspaceCall, args: Mapping[str, Any], log: _LogFields) -> WorkspaceResult:
        path = parse_workspace_path(args.get("path"), allow_folder=True)
        log.path = path
        if path == "" or path.endswith("/"):
            entries = await self._store.list(call.user_id, path, include_versions=True)
            return WorkspaceResult(text=render_listing(path, entries))
        generation = parse_generation(args.get("generation"))
        if generation is not None:
            return await self._view_version(call, path, generation, args, log)
        if workspace_kind(path) == "binary":
            live = await self._store.list(call.user_id, path)
            if not live:
                return await self._missing(call, path, log)
            return self._binary(path, live[0].generation, log)
        try:
            blob = await self._store.read(call.user_id, path)
        except FileNotFoundError:
            return await self._missing(call, path, log)
        entry = blob.entry
        log.gen, log.nbytes = str(entry.generation), entry.size
        text = _decode(blob.data)
        if text is None:
            return self._binary(path, entry.generation, log)
        window = number_lines(text, args.get("offset"), args.get("limit"))
        parts = [render_view_header(entry, window.total)]
        if call.fire_key and entry.fire == call.fire_key and call.known.get(path) != entry.generation:
            # B1: an earlier attempt of this same fire wrote it; name the version from before the fire.
            versions = await self._store.list(call.user_id, path, include_versions=True)
            parts.append(render_same_fire_line(prior_generation_before_fire(versions, call.fire_key)))
        parts += [p for p in (window.text, window_note(window)) if p]
        return WorkspaceResult(text="\n".join(parts), generations={path: entry.generation})

    @staticmethod
    def _binary(path: str, generation: int, log: _LogFields) -> WorkspaceResult:
        log.result, log.gen = "refused:binary", str(generation)
        # Recorded: `copy` may overwrite a files/ copy this execution has looked at (§7.3 copy).
        return WorkspaceResult(text=f"SYSTEM: {binary_message(path)}.", failed=True, generations={path: generation})

    async def _view_version(
        self, call: WorkspaceCall, path: str, generation: int, args: Mapping[str, Any], log: _LogFields,
    ) -> WorkspaceResult:
        blob = await self._read_version(call, path, generation)
        log.gen, log.nbytes = str(generation), blob.entry.size
        text = _decode(blob.data) if workspace_kind(path) == "text" else None
        if text is None:
            raise WorkspaceRefused(binary_message(path), reason="binary")
        window = number_lines(text, args.get("offset"), args.get("limit"))
        parts = [render_view_header(blob.entry, window.total, version_view=True)]
        parts += [p for p in (window.text, window_note(window)) if p]
        # Read-only: not recorded, so no later create can overwrite on the strength of an old version (§7.3).
        return WorkspaceResult(text="\n".join(parts))

    async def _read_version(self, call: WorkspaceCall, path: str, generation: int) -> WorkspaceBlob:
        try:
            return await self._store.read(call.user_id, path, generation=generation)
        except FileNotFoundError:
            raise WorkspaceRefused(
                f"{path} has no version {generation}; history lists them", reason="no_version",
            ) from None

    async def _missing(self, call: WorkspaceCall, path: str, log: _LogFields) -> WorkspaceResult:
        """D8: who deleted it or where it went, with its versions — reported, never recreated (§7.4)."""
        log.result = "not_found"
        versions = await self._store.list(call.user_id, path, include_versions=True)
        return WorkspaceResult(text=render_missing(path, versions))

    async def _history(self, call: WorkspaceCall, args: Mapping[str, Any], log: _LogFields) -> WorkspaceResult:
        path = parse_workspace_path(args.get("path"), allow_folder=True)
        log.path = path
        entries = await self._store.list(call.user_id, path, include_versions=True)
        return WorkspaceResult(text=render_history(path, entries))

    # -- text writes ---------------------------------------------------------------------------------------------

    @staticmethod
    def _meta(call: WorkspaceCall, restored_from: Optional[int] = None) -> WorkspaceWriteMeta:
        return WorkspaceWriteMeta(origin=call.origin, fire=call.fire_key, restored_from=restored_from)

    @staticmethod
    def _text_path(raw: object, command: str) -> str:
        """create / str_replace write text, and only under projects/, references/, scratch/ (§6)."""
        path = parse_workspace_path(raw)
        if folder_of(path) not in TEXT_FOLDERS:
            raise WorkspaceRefused(
                f"invalid workspace path '{path}': files/ holds copies only; use copy", reason="folder",
            )
        if workspace_kind(path) != "text":
            raise WorkspaceRefused(
                f"invalid workspace path '{path}': {command} writes text files only ({' '.join(TEXT_EXTENSIONS)})",
                reason="kind",
            )
        return path

    async def _create(self, call: WorkspaceCall, args: Mapping[str, Any], log: _LogFields) -> WorkspaceResult:
        path = self._text_path(args.get("path"), "create")
        log.path = path
        content = args.get("content")
        text = "" if content is None else str(content)
        data = text.encode("utf-8")
        check_file_size(path, data)
        log.nbytes = len(data)
        known = call.known.get(path)
        if known is not None:
            # This execution viewed the current version: overwrite it, guarded by that generation.
            entry = await self._store.put(call.user_id, path, data, self._meta(call), known)
            log.gen = str(entry.generation)
            return WorkspaceResult(
                text=f"overwritten workspace:{path} · generation {entry.generation} · {_lines(text)}",
                generations={path: entry.generation},
            )
        live = await self._store.list(call.user_id, "")
        if any(e.path == path for e in live):
            raise WorkspaceRefused(
                f"{path} exists. View it first and use str_replace, or view it and create again to overwrite",
                reason="exists",
            )
        check_caps(live, len(data))
        history = await self._store.list(call.user_id, path, include_versions=True)
        log.had_history = "1" if history else "0"
        entry = await self._store.put(call.user_id, path, data, self._meta(call), 0)
        log.gen = str(entry.generation)
        return WorkspaceResult(
            text=f"created workspace:{path} · generation {entry.generation} · {_lines(text)}",
            generations={path: entry.generation},
        )

    async def _str_replace(self, call: WorkspaceCall, args: Mapping[str, Any], log: _LogFields) -> WorkspaceResult:
        path = self._text_path(args.get("path"), "str_replace")
        log.path = path
        old = str(args.get("old_str") or "")
        new_raw = args.get("new_str")
        new = "" if new_raw is None else str(new_raw)
        for attempt in (1, 2):
            # Self-guarding (D13): read the current generation, write under it; on 412 re-read and re-apply once.
            blob = await self._store.read(call.user_id, path)
            text = _decode(blob.data)
            if text is None:
                raise WorkspaceRefused(f"{path} is not UTF-8 text; str_replace cannot edit it", reason="kind")
            replaced = apply_str_replace(path, text, old, new)
            data = replaced.text.encode("utf-8")
            check_file_size(path, data)
            try:
                entry = await self._store.put(call.user_id, path, data, self._meta(call), blob.entry.generation)
            except WorkspaceConflict:
                if attempt == 2:
                    raise
                continue
            log.gen, log.nbytes = str(entry.generation), len(data)
            return WorkspaceResult(
                text=f"replaced in workspace:{path} · generation {entry.generation}\n{changed_lines(replaced)}",
                generations={path: entry.generation},
            )
        raise WorkspaceConflict(path)  # unreachable: the second attempt returns or raises

    # -- delete / rename / restore -------------------------------------------------------------------------------

    @staticmethod
    def _delete_meta(call: WorkspaceCall, renamed_to: Optional[str] = None) -> WorkspaceDeleteMeta:
        return WorkspaceDeleteMeta(deleted_by=call.origin, deleted_fire=call.fire_key, renamed_to=renamed_to)

    async def _delete(self, call: WorkspaceCall, args: Mapping[str, Any], log: _LogFields) -> WorkspaceResult:
        path = parse_workspace_path(args.get("path"))
        log.path = path
        generation = call.known.get(path)
        if generation is None:
            live = await self._store.list(call.user_id, path)
            if not live:
                raise FileNotFoundError(path)
            generation = live[0].generation
        log.gen = str(generation)
        await self._store.delete(call.user_id, path, generation, self._delete_meta(call))
        lines = [f"deleted workspace:{path}; restorable from history (generation {generation})"]
        lines += find_references(path, call.references)
        return WorkspaceResult(text="\n".join(lines), generations={path: None})

    async def _rename(self, call: WorkspaceCall, args: Mapping[str, Any], log: _LogFields) -> WorkspaceResult:
        src = parse_workspace_path(args.get("path"))
        dst = parse_workspace_path(args.get("new_path"))
        log.path = src
        if dst == src:
            raise WorkspaceRefused(f"new_path is the same as path ({src})", reason="path")
        src_copy, dst_copy = folder_of(src) == COPY_FOLDER, folder_of(dst) == COPY_FOLDER
        if src_copy and not dst_copy:
            raise WorkspaceRefused(f"invalid workspace path '{dst}': a file in files/ stays in files/", reason="folder")
        if dst_copy and not src_copy:
            raise WorkspaceRefused(
                f"invalid workspace path '{dst}': a file may not move into files/ (it holds copies only)",
                reason="folder",
            )
        if not dst_copy and workspace_kind(dst) != "text":
            raise WorkspaceRefused(
                f"invalid workspace path '{dst}': projects/, references/ and scratch/ hold text files only "
                f"({' '.join(TEXT_EXTENSIONS)})",
                reason="kind",
            )
        # R5: read + put + delete, never a server-side copy (a copy would carry the old customTime).
        blob = await self._store.read(call.user_id, src)
        log.nbytes = blob.entry.size
        try:
            created = await self._store.put(call.user_id, dst, blob.data, self._meta(call), 0)
        except WorkspaceConflict:
            raise WorkspaceRefused(
                f"{dst} already exists; pick another new_path, or view and delete it first", reason="exists",
            ) from None
        try:
            await self._store.delete(
                call.user_id, src, blob.entry.generation, self._delete_meta(call, renamed_to=dst),
            )
        except Exception:
            # §5.3: undo the copy so a failed rename leaves only the source.
            try:
                await self._store.delete(call.user_id, dst, created.generation, self._delete_meta(call))
            except Exception as rollback_error:
                logger.error(
                    "❌ [Workspace] rename rollback failed user=%s src=%s dst=%s: %s",
                    call.user_id[:8], src, dst, rollback_error, exc_info=True,
                )
                log.result, log.gen = "error", str(created.generation)
                return WorkspaceResult(
                    text=(f"SYSTEM: the rename was interrupted and both workspace:{src} and workspace:{dst} exist "
                          "now. View both and delete the one that is not needed."),
                    failed=True,
                    generations={dst: created.generation},
                )
            raise
        log.gen = str(created.generation)
        lines = [f"renamed workspace:{src} to workspace:{dst} · generation {created.generation}"]
        lines += find_references(src, call.references, new_path=dst)
        return WorkspaceResult(text="\n".join(lines), generations={src: None, dst: created.generation})

    async def _restore(self, call: WorkspaceCall, args: Mapping[str, Any], log: _LogFields) -> WorkspaceResult:
        path = parse_workspace_path(args.get("path"))
        log.path = path
        generation = parse_generation(args.get("generation"))
        if generation is None:
            raise WorkspaceRefused("restore needs a generation; history lists them", reason="generation")
        blob = await self._read_version(call, path, generation)
        log.nbytes = blob.entry.size
        if blob.entry.live:
            log.gen = str(generation)
            return WorkspaceResult(
                text=f"generation {generation} is already the current version of workspace:{path}; nothing to restore",
                generations={path: generation},
            )
        live = await self._store.list(call.user_id, "")
        current = next((e for e in live if e.path == path), None)
        if current is None:
            check_caps(live, len(blob.data))  # restoring a deleted file adds a live file
        else:
            check_replace_caps(live, current.size, len(blob.data))
        entry = await self._store.put(
            call.user_id, path, blob.data, self._meta(call, restored_from=generation),
            current.generation if current else 0,
        )
        log.gen = str(entry.generation)
        return WorkspaceResult(
            text=f"restored generation {generation} as generation {entry.generation} of workspace:{path}",
            generations={path: entry.generation},
        )

    # -- copy ----------------------------------------------------------------------------------------------------

    async def _copy(self, call: WorkspaceCall, args: Mapping[str, Any], log: _LogFields) -> WorkspaceResult:
        dst = parse_workspace_path(args.get("path"))
        log.path = dst
        source = str(args.get("source") or "").strip().strip('"')
        data = await self._copy_source(call, source, dst)
        check_file_size(dst, data)
        log.nbytes = len(data)
        known = call.known.get(dst)
        if known is not None:
            live = await self._store.list(call.user_id, "")
            current = next((e for e in live if e.path == dst), None)
            if current is not None:
                check_replace_caps(live, current.size, len(data))
            entry = await self._store.put(call.user_id, dst, data, self._meta(call), known)
        else:
            live = await self._store.list(call.user_id, "")
            if any(e.path == dst for e in live):
                raise WorkspaceRefused(
                    f"{dst} exists. View it first to overwrite it with copy, or pick another path", reason="exists",
                )
            check_caps(live, len(data))
            entry = await self._store.put(call.user_id, dst, data, self._meta(call), 0)
        log.gen = str(entry.generation)
        return WorkspaceResult(
            text=f"copied {source} to workspace:{dst} · {format_size(len(data))} · generation {entry.generation}",
            generations={dst: entry.generation},
        )

    async def _copy_source(self, call: WorkspaceCall, source: str, dst: str) -> bytes:
        if not source:
            raise WorkspaceRefused(
                "copy needs a source: a chat file name, a delivered-document key or a workspace path", reason="source",
            )
        if drive_ref.is_drive_ref(source):   # tagged or not (MULTI_DRIVE_RFC §9)
            raise WorkspaceRefused(
                f"'{source}' is on the user's drive; drive files are not copied into the workspace — open it through "
                "work_with_drive_files",
                reason="wrong_store",
            )
        if source.startswith("skill:"):
            raise WorkspaceRefused(
                f"'{source}' is a skill file; read it with open_file instead of copying it", reason="wrong_store",
            )
        data = await self._read_source(call, source)
        # §6 / §0.2: text (an upload or a workspace file) may land in any folder; binary only in files/.
        if folder_of(dst) != COPY_FOLDER and not is_text(dst, data):
            raise WorkspaceRefused(
                f"invalid workspace path '{dst}': only text files go outside files/; copy binary files under files/",
                reason="kind",
            )
        return data

    async def _read_source(self, call: WorkspaceCall, source: str) -> bytes:
        if source.startswith(WORKSPACE_REF_PREFIX) or source.startswith(FOLDERS):
            src = parse_workspace_path(source)
            try:
                return (await self._store.read(call.user_id, src)).data
            except FileNotFoundError:
                raise WorkspaceRefused(
                    f"workspace:{src} does not exist; view it to see its history", reason="not_found",
                ) from None
        if self._file_conversion is None:
            raise WorkspaceRefused("copying chat files and delivered documents is unavailable here", reason="source")
        try:
            return await self._file_conversion.resolve_bytes(source, call.user_id)
        except FileNotFoundError:
            raise WorkspaceRefused(
                f"'{source}' not found — pass the file name from its [File: …] label, or the delivered-document key",
                reason="source",
            ) from None
        except PermissionError:
            raise WorkspaceRefused(f"'{source}' does not belong to this user", reason="source") from None
