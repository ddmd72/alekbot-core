# User Drive — Step 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot works with a long-term file area in the owner's OneDrive App Folder — save (readable names, no overwrite), list, search, open, move/rename, create folders, append/replace, delete — with a chat receipt for every destructive action and no provider name visible to the model.

**Architecture:** A domain concept "user drive" (`src/domain/user_drive.py`) behind `UserDrivePort`; `OneDriveAdapter` is its first adapter (Graph REST over aiohttp, token refresh + in-memory cache shared with To Do). `UserDriveService` holds the rules (folder resolution, duplicate handling, append, root protection, subtree count, bounds). The zero-LLM `FileManagementAgent` gets eight drive intents, runs every mutation shielded from cancellation, and posts receipts; the coordinator stops pre-fetching its `file_ref`s.

**Tech Stack:** Python 3.13, aiohttp, Quart, pytest + pytest-asyncio (`asyncio_mode=auto`), Microsoft Graph v1.0 (consumers tenant), Firestore (prompt token), Cloud Run.

**Spec:** `docs/10_rfcs/USER_DRIVE_RFC.md` (revision 6). Read it fully before Task 1. Section numbers below (§N) refer to it.

## Global Constraints

- Ref prefix `drive:` + opaque item id. The words `OneDrive`/`onedrive`/`Microsoft`/`Google` never appear in a ref, a label, an intent name or description, a `context_schemas` text, or a receipt template (§4.3).
- Labels: file `[Drive: <path> (<size>) ref=drive:<id>]`; folder `[Drive folder: <path>/ ref=drive:<id>]`; root renders as `/` (§4.3).
- Paths shown to the model are relative to the area root and **unquoted** (Graph returns them percent-encoded) (§4.3).
- Default folder `Inbox` (`DEFAULT_INBOX_FOLDER`) (§4.7).
- Intent names, exactly: `save_file_to_drive`, `list_files_in_drive`, `search_files_in_drive`, `open_file_from_drive`, `move_file_in_drive`, `create_folder_in_drive`, `update_file_in_drive`, `delete_file_from_drive` (§3).
- `open_file` / `delete_file` keep their behaviour for non-drive refs. Mutating intents are strict about the store; reading is lenient (§4.4).
- `FILE_MANAGEMENT.prefetch_file_ref = False` (§4.4).
- OAuth scope `Files.ReadWrite.AppFolder offline_access`; credentials provider key `microsoft_onedrive` (§4.1).
- Caps, checked on metadata **before** any download (§4.6): any download `MAX_DRIVE_DOWNLOAD_BYTES = 45 MiB`; text conversion `MAX_FILE_BYTES` (5 MB, `src/utils/file_conversion.py:23`); vision images `MAX_DRIVE_VISION_IMAGE_BYTES = 5 MiB`, vision PDFs `MAX_DRIVE_VISION_PDF_BYTES = 20 MiB`; append target `MAX_DRIVE_APPEND_FILE_BYTES = 5 MiB`.
- Saves never overwrite: same name + same bytes → existing returned; same name + different bytes → provider suffix, actual name reported (§4.7).
- `update_file_in_drive` modes: `append_text` (text files) or `source_ref` (replace). No full-text overwrite (§4.9).
- Receipts via `UserNotificationService.notify_raw` for delete (file / folder with count) and replace (sizes); none for save, move, rename, create, append (§4.10).
- Every mutation and its receipt run inside `asyncio.shield`; the agent waits for it at most 100 s and otherwise answers "still running"; one user's mutations run one at a time; agent timeout 120 s (§4.13).
- Token cache lives at most 5 minutes (§4.2).
- Vision only for JPEG/PNG/GIF/WebP images and PDFs (§4.6).
- Names are compared with Unicode NFC + casefold (`name_key`) (§4.7).
- Drive intents return `history_context={"drive_context": [{"path": ..., "ref": ...}, ...]}` (§4.3).
- Expired / revoked access → `DriveNotConnectedError` → "connect your drive in the Cabinet" (§4.2).
- Import rules (root `CLAUDE.md`): domain = stdlib only; services import ports, never adapters or other services at runtime; composition wires concretes.
- `make check` stays green: ruff on `src/`, `mypy --strict` on `src/domain` + `src/ports`.
- **Reviewer rule:** an existing test that fails is never edited by the implementer — stop, hand it to the reviewer (root `CLAUDE.md` → Tests). New behaviour gets new tests in new files or classes.
- No secrets, URLs or ids in tracked files (root `CLAUDE.md` → SECRETS RULE). All code, comments, docs in English.

## Review Focus

1. **A ref copied with label punctuation** (`"drive:abc"`, `drive:abc]`) → still resolves. Task 1 `test_parse_tolerates_label_punctuation`.
2. **A Cyrillic or spaced folder path, percent-encoded by Graph** → shown readable, never `%D0%92…`, never mislabelled as top level. Task 4 `TestPaths`.
3. **"Delete" or "move" pointed at the area root** → refused. Task 5 `test_delete_root_refused`, `test_move_root_refused`.
4. **The same file saved twice (or a retried save after a timeout)** → one copy, reply says it was already there. Task 5 `TestSaveDuplicates`.
5. **Access expired at Microsoft** → "reconnect in the Cabinet", not a stack trace or a GCS fallback. Task 3 `test_invalid_grant_raises_reauth`, Task 4 `test_401_after_forced_refresh_is_not_connected`, Task 8 `test_not_connected_message`.

---

## Branch and worktree (before Task 0)

Parallel sessions share the main worktree (memory `feedback_shared_worktree_parallel_sessions`). Create a separate one (`superpowers:using-git-worktrees`):

```bash
git worktree add ../alekbot-core-user-drive -b feat/user-drive main
cp docs/10_rfcs/USER_DRIVE_RFC.md ../alekbot-core-user-drive/docs/10_rfcs/
mkdir -p ../alekbot-core-user-drive/docs/superpowers/plans
cp docs/superpowers/plans/2026-10-09-user-drive-step1.md ../alekbot-core-user-drive/docs/superpowers/plans/
cd ../alekbot-core-user-drive
git add docs/10_rfcs/USER_DRIVE_RFC.md   # docs/superpowers/ is gitignored: the plan stays a local file
git commit -m "docs(drive): USER_DRIVE_RFC revision 6"
```

Then delete the uncommitted RFC copy from the main worktree (keep the plan there too, or work from the worktree copy). Merge only when the whole branch is done (memory `feedback_merge_only_when_fully_done`); the owner merges.

---

## Task 0: Phase 0 spike — verify Graph behaviour on the owner's account

**Owner prerequisites (ask, wait for confirmation):**
1. Azure → App registrations → `Alek-bot` → API permissions → Microsoft Graph → Delegated → `Files.ReadWrite.AppFolder`.
2. Same registration → Authentication → Web → redirect URI `http://localhost:8765/callback` (removed after this task).

**Files:**
- Create: `scripts/onedrive/probe_appfolder.py`
- Modify: `docs/10_rfcs/USER_DRIVE_RFC.md` (§7 "Spike results")

**Assumptions confirmed here (later tasks rely on them):**
- A1 `parentReference.path` is present on GET item, `/children`, and on the PATCH / PUT / POST responses; record whether search results carry it.
- A1b The path is percent-encoded (Cyrillic and spaces), and `unquote` of it equals the readable path.
- A2 `PUT …/content` accepts ≤ 4 MiB.
- A3 `PATCH` move + rename keeps the id.
- A4 `DELETE` of a non-empty folder → 204; restorable whole from the recycle bin (manual).
- A5 GET item returns `@microsoft.graph.downloadUrl`.
- A6 Search inside the App Folder returns items from nested subfolders; record name/content match and lag.
- A7 `PUT …/content` on an existing item adds a version.
- A8 The name `conflictBehavior=rename` produces for a clash (e.g. `a 1.txt`).

- [ ] **Step 1: Write the probe script**

```python
#!/usr/bin/env python3
"""
Phase 0 spike for docs/10_rfcs/USER_DRIVE_RFC.md — probes Graph behaviour in the
owner's OneDrive App Folder before any adapter code is written.

Interactive: opens the browser for consent (auth-code flow, localhost redirect),
keeps tokens in memory only, writes a JSON report to scripts/memory/ (gitignored).

Prerequisites: delegated Files.ReadWrite.AppFolder on the app registration and the
redirect URI http://localhost:8765/callback registered on it.

    python scripts/onedrive/probe_appfolder.py
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import quote, unquote, urlencode

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

_AUTH = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
_GRAPH = "https://graph.microsoft.com/v1.0"
_REDIRECT = "http://localhost:8765/callback"
_SCOPE = "Files.ReadWrite.AppFolder offline_access"
_OUT = Path(__file__).resolve().parents[1] / "memory"


async def _get_code(client_id: str) -> str:
    state = secrets.token_urlsafe(16)
    got: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def callback(request: web.Request) -> web.Response:
        if request.query.get("state") != state:
            got.set_exception(RuntimeError("state mismatch"))
        elif "error" in request.query:
            got.set_exception(RuntimeError(request.query.get("error_description", "denied")))
        else:
            got.set_result(request.query["code"])
        return web.Response(text="Probe authorised. You can close this tab.")

    app = web.Application()
    app.router.add_get("/callback", callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", 8765).start()
    params = {"client_id": client_id, "response_type": "code", "redirect_uri": _REDIRECT,
              "scope": _SCOPE, "state": state, "response_mode": "query"}
    webbrowser.open(f"{_AUTH}/authorize?{urlencode(params)}")
    try:
        return await asyncio.wait_for(got, timeout=300)
    finally:
        await runner.cleanup()


def _pp(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"raw": raw}
    path = (raw.get("parentReference") or {}).get("path")
    return {"name": raw.get("name"), "parent_path": path,
            "parent_path_unquoted": unquote(path) if path else None}


async def main() -> int:
    client_id = os.environ["MICROSOFT_TODO_CLIENT_ID"].strip()
    secret = os.environ["MICROSOFT_TODO_CLIENT_SECRET"].strip()
    code = await _get_code(client_id)
    report: Dict[str, Any] = {"at": datetime.now().isoformat()}
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{_AUTH}/token", data={
            "client_id": client_id, "client_secret": secret, "code": code,
            "redirect_uri": _REDIRECT, "grant_type": "authorization_code", "scope": _SCOPE,
        }) as r:
            tok = await r.json()
            if r.status != 200:
                raise RuntimeError(f"token exchange failed: {tok}")
        h = {"Authorization": f"Bearer {tok['access_token']}"}

        async def call(method: str, url: str, **kw: Any) -> Tuple[int, Any]:
            async with s.request(method, url if url.startswith("http") else _GRAPH + url,
                                 headers={**h, **kw.pop("headers", {})}, **kw) as r:
                text = await r.text()
                try:
                    return r.status, (json.loads(text) if text else None)
                except ValueError:
                    return r.status, text

        st, root = await call("GET", "/me/drive/special/approot")
        report["root"] = {"status": st, "id": root.get("id"), **_pp(root)}
        root_id = root["id"]

        st, probe = await call("POST", f"/me/drive/items/{root_id}/children",
                               json={"name": f"Проба {stamp}", "folder": {},
                                     "@microsoft.graph.conflictBehavior": "fail"})
        report["A1_post_folder"] = {"status": st, **_pp(probe)}
        probe_id = probe["id"]
        st, sub = await call("POST", f"/me/drive/items/{probe_id}/children",
                             json={"name": "Встречи 2026", "folder": {}})
        report["A1_post_subfolder"] = {"status": st, **_pp(sub)}
        sub_id = sub["id"]

        name = f"заметка {stamp}.txt"
        st, small = await call("PUT", f"/me/drive/items/{sub_id}:/{quote(name)}:/content",
                               data=f"contentuniq{stamp} привет".encode(),
                               headers={"Content-Type": "text/plain"})
        report["A1_put_new"] = {"status": st, **_pp(small)}
        file_id = small["id"]
        st, _ = await call("PUT", f"/me/drive/items/{sub_id}:/big.bin:/content",
                           data=b"0" * (4 * 1024 * 1024), headers={"Content-Type": "application/octet-stream"})
        report["A2_4mib_put_status"] = st

        st, clash = await call("PUT", f"/me/drive/items/{sub_id}:/{quote(name)}:/content"
                                      "?@microsoft.graph.conflictBehavior=rename",
                               data=b"other", headers={"Content-Type": "text/plain"})
        report["A8_rename_clash_name"] = clash.get("name") if isinstance(clash, dict) else clash

        st, item = await call("GET", f"/me/drive/items/{file_id}")
        report["A1_get_item"] = _pp(item)
        report["A5_download_url_present"] = "@microsoft.graph.downloadUrl" in item
        st, kids = await call("GET", f"/me/drive/items/{sub_id}/children")
        report["A1_children"] = [_pp(k) for k in kids.get("value", [])]

        for label, q in (("name", f"заметка {stamp}"), ("content", f"contentuniq{stamp}")):
            hits: Any = []
            waited = 0
            for _ in range(12):  # search indexing lags; poll up to ~60 s
                st, res = await call("GET", f"/me/drive/special/approot/search(q='{quote(q)}')")
                hits = res.get("value", []) if isinstance(res, dict) else []
                if hits:
                    break
                await asyncio.sleep(5)
                waited += 5
            report[f"A6_search_{label}"] = {"waited_s": waited, "hits": [_pp(x) for x in hits]}

        st, moved = await call("PATCH", f"/me/drive/items/{file_id}",
                               json={"parentReference": {"id": probe_id}, "name": f"переименовано {name}"})
        report["A3_patch"] = {"status": st, "id_kept": moved.get("id") == file_id, **_pp(moved)}

        st, replaced = await call("PUT", f"/me/drive/items/{file_id}/content", data=b"second version",
                                  headers={"Content-Type": "text/plain"})
        report["A1_put_replace"] = {"status": st, **_pp(replaced)}
        st, versions = await call("GET", f"/me/drive/items/{file_id}/versions")
        report["A7_versions"] = len(versions.get("value", [])) if isinstance(versions, dict) else versions

        st, _ = await call("DELETE", f"/me/drive/items/{probe_id}")
        report["A4_delete_nonempty_folder_status"] = st
        st, _ = await call("GET", f"/me/drive/items/{probe_id}")
        report["A4_get_after_delete_status"] = st

    _OUT.mkdir(parents=True, exist_ok=True)
    out = _OUT / f"onedrive_probe_{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\nReport → {out}")
    print(f"Manual check: restore folder 'Проба {stamp}' from the OneDrive recycle bin, confirm "
          f"'Встречи 2026' and the renamed file come back, then delete it again.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Run it with the owner (browser consent)**

Run: `python scripts/onedrive/probe_appfolder.py`
Expected: consent page for `Alek-bot`, then a JSON report; `Apps/Alek-bot` now exists in OneDrive.

- [ ] **Step 3: Owner restores the probe folder from the recycle bin, checks the subtree, deletes it again; removes the localhost redirect URI.**

- [ ] **Step 4: Record results in RFC §7 ("Spike results (YYYY-MM-DD)") — one line per assumption, observed values.**

Decision points: **A1/A1b/A3/A5 failing → STOP and report before Task 4.** A6 failing (search returns nothing inside the area) → the RFC already decides: remove `search_files_in_drive` from Tasks 7, 8, 11, 12 and note it in the RFC. A6 passing → pick the sentence for Task 7's description ("Matches file names." or "Matches file names and content."). A8 → put the observed suffix format into Task 4's `test_upload_uses_conflict_rename` comment and Task 5's `test_same_name_different_size_uploaded_renamed` fixture name.

- [ ] **Step 5: Commit**

```bash
git add scripts/onedrive/probe_appfolder.py docs/10_rfcs/USER_DRIVE_RFC.md
git commit -m "spike(drive): probe OneDrive App Folder behaviour (USER_DRIVE_RFC Phase 0)"
```

---

## Task 1: Domain — items, refs, names, paths, labels, outcomes, errors, caps

**Files:**
- Create: `src/domain/user_drive.py`
- Test: `tests/unit/domain/test_user_drive.py`

**Interfaces — produces:**
- Constants: `DRIVE_REF_PREFIX`, `DEFAULT_INBOX_FOLDER`, `MAX_DRIVE_DOWNLOAD_BYTES`, `MAX_DRIVE_VISION_IMAGE_BYTES`, `MAX_DRIVE_VISION_PDF_BYTES`, `MAX_DRIVE_APPEND_FILE_BYTES`, `TEXT_FILE_EXTENSIONS`, `VISION_IMAGE_MIME_TYPES`.
- Errors: `DriveNotConnectedError`, `DriveItemNotFoundError(FileNotFoundError)`, `DriveRootProtectedError`, `DrivePathError(ValueError)`, `DriveNameConflictError`, `DriveFileTooLargeError(item, limit_bytes)`.
- `DriveItem` (frozen dataclass): `item_id, name, path, is_folder, size_bytes=0, mime_type="", modified_at=None, web_url="", child_count=0`; property `ref`.
- Outcomes (dataclasses): `FolderResolution(folder, created)`, `SaveOutcome(item, created, already_existed=False, renamed=False)`, `ListOutcome(folder, items, truncated)`, `MoveOutcome(before, after, created)`, `DeleteOutcome(item, file_count, count_capped=False)`, `UpdateOutcome(item, before_size, mode)` — `mode` is `"append"` or `"replace"`.
- Functions: `make_drive_ref`, `parse_drive_ref`, `is_drive_ref`, `split_folder_path`, `name_key(name)`, `match_child_folder`, `join_drive_path`, `format_size`, `drive_label`, `drive_context_entry`, `sanitize_drive_filename`, `drive_filename(requested, source_ref)`, `is_text_file(name, mime_type)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Domain rules for the user's drive (docs/10_rfcs/USER_DRIVE_RFC.md §4.3, §4.6–§4.9)."""
import pytest

from src.domain.user_drive import (
    DEFAULT_INBOX_FOLDER,
    DriveItem,
    DriveItemNotFoundError,
    DrivePathError,
    drive_context_entry,
    drive_filename,
    drive_label,
    is_drive_ref,
    is_text_file,
    join_drive_path,
    make_drive_ref,
    match_child_folder,
    name_key,
    parse_drive_ref,
    sanitize_drive_filename,
    split_folder_path,
)


def _folder(name: str, item_id: str = "f1", path: str = "") -> DriveItem:
    return DriveItem(item_id=item_id, name=name, path=path or name, is_folder=True)


class TestRefs:
    def test_round_trip(self):
        assert parse_drive_ref(make_drive_ref("ABC!123")) == "ABC!123"

    def test_non_drive_refs(self):
        for ref in ("report.docx", "docs/u1/x.pdf", "skill:a/b.md", "", "drive:"):
            assert parse_drive_ref(ref) is None
            assert is_drive_ref(ref) is False

    def test_parse_tolerates_label_punctuation(self):
        assert parse_drive_ref('"drive:abc"') == "abc"
        assert parse_drive_ref("drive:abc]") == "abc"
        assert parse_drive_ref(" ref=drive:abc ") == "abc"


class TestSplitFolderPath:
    @pytest.mark.parametrize("raw,expected", [
        ("Встречи/2026", ["Встречи", "2026"]),
        ("/Встречи/2026/ ", ["Встречи", "2026"]),
        ("Встречи//2026", ["Встречи", "2026"]),
        ("  Inbox ", ["Inbox"]),
        ("", []),
        ("/", []),
    ])
    def test_normalises(self, raw, expected):
        assert split_folder_path(raw) == expected

    @pytest.mark.parametrize("raw", ["../x", "a/../b", "a/./b", "a\\b"])
    def test_rejects_escape_and_backslash(self, raw):
        with pytest.raises(DrivePathError):
            split_folder_path(raw)


class TestMatchChildFolder:
    def test_case_insensitive(self):
        assert match_child_folder([_folder("Встречи", "f1")], "встречи").item_id == "f1"

    def test_exact_case_wins(self):
        assert match_child_folder([_folder("docs", "a"), _folder("Docs", "b")], "Docs").item_id == "b"

    def test_files_are_not_folders(self):
        kids = [DriveItem(item_id="x", name="Встречи", path="Встречи", is_folder=False)]
        assert match_child_folder(kids, "Встречи") is None

    def test_nfd_name_matches_nfc(self):
        import unicodedata
        nfd = unicodedata.normalize("NFD", "Мой отчёт")
        assert name_key(nfd) == name_key("мой отчёт")
        assert match_child_folder([_folder(nfd, "f9")], "Мой отчёт").item_id == "f9"


class TestNames:
    def test_strips_delivered_uuid_prefix(self):
        ref = "docs/u1/3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b-Отчёт за май.docx"
        assert drive_filename(None, ref) == "Отчёт за май.docx"

    def test_chat_upload_keeps_name(self):
        assert drive_filename(None, "image (3).png") == "image (3).png"

    def test_requested_name_gets_source_extension(self):
        assert drive_filename("Договор аренды", "lease.pdf") == "Договор аренды.pdf"
        assert drive_filename("Договор v2.1", "lease.pdf") == "Договор v2.1.pdf"
        assert drive_filename("lease.PDF", "lease.pdf") == "lease.PDF"
        assert drive_filename("notes.md", "x.txt") == "notes.md"

    def test_forbidden_characters(self):
        assert sanitize_drive_filename('a/b:c*d?"e<f>g|h\\i.txt') == "a_b_c_d__e_f_g_h_i.txt"
        assert sanitize_drive_filename("   ...  ") == "file"

    def test_text_files(self):
        assert is_text_file("a.md", "") and is_text_file("a.bin", "text/plain")
        assert not is_text_file("a.pdf", "application/pdf")


class TestLabels:
    def test_file_label(self):
        item = DriveItem(item_id="id1", name="a.m4a", path="Встречи/a.m4a", is_folder=False, size_bytes=12_900_000)
        assert drive_label(item) == "[Drive: Встречи/a.m4a (12.3MB) ref=drive:id1]"

    def test_folder_and_root(self):
        assert drive_label(_folder("Встречи", "f1")) == "[Drive folder: Встречи/ ref=drive:f1]"
        root = DriveItem(item_id="r", name="Alek-bot", path="", is_folder=True)
        assert drive_label(root) == "[Drive folder: / ref=drive:r]"

    def test_context_entry(self):
        item = DriveItem(item_id="id1", name="a.txt", path="a.txt", is_folder=False)
        assert drive_context_entry(item) == {"path": "a.txt", "ref": "drive:id1"}


def test_join_inbox_and_not_found():
    assert join_drive_path("", "a.txt") == "a.txt"
    assert join_drive_path("Встречи", "a.txt") == "Встречи/a.txt"
    assert DEFAULT_INBOX_FOLDER == "Inbox"
    assert issubclass(DriveItemNotFoundError, FileNotFoundError)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/domain/test_user_drive.py -v` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
"""
User drive — the user's long-term file area (docs/10_rfcs/USER_DRIVE_RFC.md).

A domain concept, not a provider: nothing here (refs, labels, names, errors) names the
storage provider. The adapter behind UserDrivePort owns the provider's ids and paths;
the model and session history only ever see `drive:<opaque id>` refs (§4.3).
"""
from __future__ import annotations

import mimetypes
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

DRIVE_REF_PREFIX = "drive:"
DEFAULT_INBOX_FOLDER = "Inbox"

# Size caps, all checked on metadata before any download (§4.6, §4.9).
MAX_DRIVE_DOWNLOAD_BYTES = 45 * 1024 * 1024       # = FileManagementAgent's video re-send cap
MAX_DRIVE_VISION_IMAGE_BYTES = 5 * 1024 * 1024    # below the strictest provider image limit
MAX_DRIVE_VISION_PDF_BYTES = 20 * 1024 * 1024     # below the strictest provider PDF/inline limit
MAX_DRIVE_APPEND_FILE_BYTES = 5 * 1024 * 1024

TEXT_FILE_EXTENSIONS = (".md", ".txt", ".csv", ".json", ".yaml", ".yml")
# Image formats every LLM provider accepts as vision input (§4.6). HEIC (iPhone default) is not one.
VISION_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

# A ref copied out of a label may carry the label's punctuation: `ref=drive:x]`, `"drive:x"`.
_REF_RE = re.compile(r"drive:([^\s\"'\]\[)(,]+)")
# Delivered documents are keyed {prefix}/{user_id}/{uuid4}-{filename} (DocumentDeliveryService).
_UUID_PREFIX_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-", re.I)
# Characters OneDrive and most drives forbid in a name, plus control characters.
_FORBIDDEN_RE = re.compile(r'["*:<>?/\\|\x00-\x1f]')


class DriveNotConnectedError(Exception):
    """No usable access: not connected, or the access expired / was revoked."""


class DriveItemNotFoundError(FileNotFoundError):
    """The item does not exist (deleted, or outside the app area)."""


class DriveRootProtectedError(Exception):
    """An operation would delete or move the area root."""


class DrivePathError(ValueError):
    """A request the drive rules refuse (bad path, wrong item kind, wrong mode)."""


class DriveNameConflictError(Exception):
    """The provider refused a create because the name is taken."""


@dataclass(frozen=True)
class DriveItem:
    """One file or folder in the area. `path` is relative to the area root ('' for the root)."""
    item_id: str
    name: str
    path: str
    is_folder: bool
    size_bytes: int = 0
    mime_type: str = ""
    modified_at: Optional[datetime] = None
    web_url: str = ""
    child_count: int = 0

    @property
    def ref(self) -> str:
        return make_drive_ref(self.item_id)


class DriveFileTooLargeError(Exception):
    """A download was refused before fetching because the file exceeds a cap."""

    def __init__(self, item: DriveItem, limit_bytes: int) -> None:
        super().__init__(f"{item.path} is {format_size(item.size_bytes)} (limit {format_size(limit_bytes)})")
        self.item = item
        self.limit_bytes = limit_bytes


@dataclass
class FolderResolution:
    folder: DriveItem
    created: List[str] = field(default_factory=list)


@dataclass
class SaveOutcome:
    item: DriveItem
    created: List[str]
    already_existed: bool = False
    renamed: bool = False


@dataclass
class ListOutcome:
    folder: DriveItem
    items: List[DriveItem]
    truncated: bool


@dataclass
class MoveOutcome:
    before: DriveItem
    after: DriveItem
    created: List[str]


@dataclass
class DeleteOutcome:
    item: DriveItem
    file_count: int
    count_capped: bool = False


@dataclass
class UpdateOutcome:
    item: DriveItem
    before_size: int
    mode: str  # "append" | "replace"


def make_drive_ref(item_id: str) -> str:
    return f"{DRIVE_REF_PREFIX}{item_id}"


def parse_drive_ref(ref: str) -> Optional[str]:
    """Item id from a `drive:` ref, tolerating label punctuation; None for any other ref."""
    if not ref or DRIVE_REF_PREFIX not in ref:
        return None
    match = _REF_RE.search(ref)
    return match.group(1) if match else None


def is_drive_ref(ref: str) -> bool:
    return parse_drive_ref(ref) is not None


def split_folder_path(path: str) -> List[str]:
    """Normalise a user-typed folder path into segments (§4.8)."""
    if "\\" in path:
        raise DrivePathError(f"Use '/' between folders: {path!r}")
    segments = [s.strip() for s in path.split("/") if s.strip()]
    for segment in segments:
        if segment in (".", ".."):
            raise DrivePathError(f"'{segment}' is not a folder name")
    return segments


def name_key(name: str) -> str:
    """Comparison key for names: Unicode NFC + case folding. Files from macOS/iOS arrive in NFD (§4.7)."""
    return unicodedata.normalize("NFC", name).casefold()


def match_child_folder(children: Sequence[DriveItem], name: str) -> Optional[DriveItem]:
    """The child folder named `name`: exact match first, else by `name_key` (§4.8)."""
    folders = [c for c in children if c.is_folder]
    for child in folders:
        if child.name == name:
            return child
    wanted = name_key(name)
    for child in folders:
        if name_key(child.name) == wanted:
            return child
    return None


def join_drive_path(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def format_size(size_bytes: int) -> str:
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f}MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f}KB"
    return f"{size_bytes}B"


def drive_label(item: DriveItem) -> str:
    """The label the model sees (§4.3). Never names a provider."""
    if item.is_folder:
        shown = f"{item.path}/" if item.path else "/"
        return f"[Drive folder: {shown} ref={item.ref}]"
    return f"[Drive: {item.path} ({format_size(item.size_bytes)}) ref={item.ref}]"


def drive_context_entry(item: DriveItem) -> Dict[str, str]:
    """One entry of the `drive_context` history block (§4.3)."""
    return {"path": item.path or "/", "ref": item.ref}


def sanitize_drive_filename(name: str) -> str:
    cleaned = _FORBIDDEN_RE.sub("_", name).strip().rstrip(". ")
    return cleaned or "file"


def drive_filename(requested: Optional[str], source_ref: str) -> str:
    """The name a saved file gets (§4.7): requested if given, else the source name without
    a delivered-document uuid prefix. The source extension is appended unless the requested
    name already has a known file extension — "Договор v2.1" must not lose ".pdf" to a dotted
    version, "notes.md" keeps its own."""
    source_name = _UUID_PREFIX_RE.sub("", source_ref.rsplit("/", 1)[-1])
    if not requested or not requested.strip():
        return sanitize_drive_filename(source_name)
    name = sanitize_drive_filename(requested)
    source_ext = os.path.splitext(source_name)[1]
    if source_ext and mimetypes.guess_type(name)[0] is None:
        name = f"{name}{source_ext}"
    return name


def is_text_file(name: str, mime_type: str) -> bool:
    return mime_type.startswith("text/") or name.lower().endswith(TEXT_FILE_EXTENSIONS)
```

- [ ] **Step 4: Run tests + strict typing**

Run: `pytest tests/unit/domain/test_user_drive.py -v && make typecheck` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/domain/user_drive.py tests/unit/domain/test_user_drive.py
git commit -m "feat(drive): domain model for the user's drive"
```

---

## Task 2: Port — `UserDrivePort`

**Files:**
- Create: `src/ports/user_drive_port.py`
- Test: `tests/unit/ports/test_user_drive_port.py`

**Interfaces — produces:** the methods below; Task 4 implements, Tasks 5–6 call.

- [ ] **Step 1: Failing test**

```python
from src.ports.user_drive_port import UserDrivePort

_EXPECTED = {
    "display_name", "is_connected", "disconnect", "get_root", "get_item", "list_children",
    "search", "download", "upload", "replace_content", "move", "create_folder", "delete",
}


def test_port_is_abstract_with_expected_methods():
    assert _EXPECTED <= set(UserDrivePort.__abstractmethods__)
```

- [ ] **Step 2:** `pytest tests/unit/ports/test_user_drive_port.py -v` → FAIL.

- [ ] **Step 3: Implement**

```python
"""
UserDrivePort — the user's long-term file area (docs/10_rfcs/USER_DRIVE_RFC.md §4.2).

Provider-neutral: ids are opaque, paths are relative to the area root and readable
(never percent-encoded). An implementation raises DriveNotConnectedError when access is
missing or expired, DriveItemNotFoundError for a missing item and DriveNameConflictError
when a create hits a taken name.
"""
from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.user_drive import DriveItem


class UserDrivePort(ABC):

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human name of the provider, for the Cabinet only."""

    @abstractmethod
    async def is_connected(self, user_id: str) -> bool:
        """True when credentials are stored (token validity is not checked)."""

    @abstractmethod
    async def disconnect(self, user_id: str) -> None:
        """Forget the user's credentials."""

    @abstractmethod
    async def get_root(self, user_id: str) -> DriveItem:
        """The area root folder (created by the provider on first access)."""

    @abstractmethod
    async def get_item(self, user_id: str, item_id: str) -> DriveItem:
        """Metadata of one item, path included."""

    @abstractmethod
    async def list_children(self, user_id: str, folder_id: str) -> List[DriveItem]:
        """All direct children of a folder (every page)."""

    @abstractmethod
    async def search(self, user_id: str, query: str, limit: int) -> List[DriveItem]:
        """Items in the area matching `query`, at most `limit`, paths included."""

    @abstractmethod
    async def download(self, user_id: str, item_id: str) -> bytes:
        """File content. Callers check size from get_item first."""

    @abstractmethod
    async def upload(self, user_id: str, parent_id: str, filename: str, data: bytes, content_type: str) -> DriveItem:
        """New file in `parent_id`; never overwrites (a clash gets a provider-suffixed name)."""

    @abstractmethod
    async def replace_content(self, user_id: str, item_id: str, data: bytes, content_type: str) -> DriveItem:
        """Overwrite an existing file's content, keeping its id and name."""

    @abstractmethod
    async def move(self, user_id: str, item_id: str,
                   new_parent_id: Optional[str] = None, new_name: Optional[str] = None) -> DriveItem:
        """Move and/or rename; the id is unchanged."""

    @abstractmethod
    async def create_folder(self, user_id: str, parent_id: str, name: str) -> DriveItem:
        """Create one folder; DriveNameConflictError if the name is taken."""

    @abstractmethod
    async def delete(self, user_id: str, item_id: str) -> None:
        """Delete a file or folder (the provider keeps it recoverable)."""
```

- [ ] **Step 4:** `pytest tests/unit/ports/test_user_drive_port.py -v && make typecheck` → PASS.

- [ ] **Step 5: Commit** — `git add src/ports/user_drive_port.py tests/unit/ports/test_user_drive_port.py && git commit -m "feat(drive): UserDrivePort"`

---

## Task 3: Shared Microsoft Graph token provider — cache, expiry, revocation

**Files:**
- Create: `src/adapters/microsoft_graph_auth.py`
- Modify: `src/adapters/microsoft_todo_adapter.py` (`__init__`, `_get_headers`, `_refresh_token`, lines ~113–157)
- Test: `tests/unit/adapters/test_microsoft_graph_auth.py`
- Must stay green unchanged: `tests/unit/adapters/test_microsoft_todo_adapter.py`

**Interfaces — produces:**
- `GraphReauthRequired(ValueError)` — refresh answered `invalid_grant` (a `ValueError` subclass, so To Do's behaviour is unchanged).
- `MicrosoftGraphTokenProvider(oauth, client_id, client_secret, provider, scope)`:
  - `async headers(user_id, *, force_refresh=False) -> Optional[Dict[str, str]]` — `None` when not connected; cached in memory for at most `CACHE_TTL` = 5 min and never past 5 min before expiry; per-user `asyncio.Lock` around read + refresh;
  - `async refresh(creds) -> OAuthCredentials`;
  - `invalidate(user_id) -> None`.
- **Keep `import aiohttp` and call `aiohttp.ClientSession(...)`** — the To Do tests patch `aiohttp.ClientSession`.

- [ ] **Step 1: Failing tests**

```python
"""Wire tests for MicrosoftGraphTokenProvider. Mock boundary: aiohttp.ClientSession."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.microsoft_graph_auth import GraphReauthRequired, MicrosoftGraphTokenProvider
from src.domain.email import OAuthCredentials
from src.ports.oauth_credentials_port import OAuthCredentialsPort


def _creds(delta: timedelta, token: str = "old") -> OAuthCredentials:
    return OAuthCredentials(user_id="u1", provider="microsoft_onedrive", access_token=token,
                            refresh_token="r1", token_expiry=datetime.now(timezone.utc) + delta,
                            scopes=[], email_address="")


def _resp(json_data, status=200):
    r = MagicMock()
    r.status = status
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=str(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(post_resp):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.post.return_value = post_resp
    return s


def _provider(creds):
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get_credentials.return_value = creds
    return MicrosoftGraphTokenProvider(oauth, "cid", "csecret", "microsoft_onedrive",
                                       "Files.ReadWrite.AppFolder offline_access"), oauth


class TestTokenProvider:
    async def test_none_when_not_connected(self):
        provider, _ = _provider(None)
        assert await provider.headers("u1") is None

    async def test_valid_token_cached_one_store_read(self):
        provider, oauth = _provider(_creds(timedelta(hours=1)))
        assert await provider.headers("u1") == {"Authorization": "Bearer old"}
        await provider.headers("u1")
        assert oauth.get_credentials.await_count == 1
        oauth.save_credentials.assert_not_called()

    async def test_expiring_token_refreshed_with_own_scope_and_provider(self):
        provider, oauth = _provider(_creds(timedelta(minutes=1)))
        session = _session(_resp({"access_token": "new", "expires_in": 3600, "refresh_token": "r2"}))
        with patch("aiohttp.ClientSession", return_value=session):
            assert await provider.headers("u1") == {"Authorization": "Bearer new"}
        assert session.post.call_args.kwargs["data"]["scope"] == "Files.ReadWrite.AppFolder offline_access"
        saved = oauth.save_credentials.call_args.args[0]
        assert saved.provider == "microsoft_onedrive" and saved.refresh_token == "r2"

    async def test_cache_expires_after_ttl(self):
        provider, oauth = _provider(_creds(timedelta(hours=1)))
        await provider.headers("u1")
        token, expiry, _ = provider._cache["u1"]
        provider._cache["u1"] = (token, expiry, datetime.now(timezone.utc) - timedelta(minutes=6))
        await provider.headers("u1")
        assert oauth.get_credentials.await_count == 2  # a Cabinet disconnect is seen within 5 minutes

    async def test_force_refresh_ignores_cache(self):
        provider, _ = _provider(_creds(timedelta(hours=1)))
        await provider.headers("u1")
        session = _session(_resp({"access_token": "fresh", "expires_in": 3600}))
        with patch("aiohttp.ClientSession", return_value=session):
            assert await provider.headers("u1", force_refresh=True) == {"Authorization": "Bearer fresh"}

    async def test_invalid_grant_raises_reauth(self):
        provider, _ = _provider(_creds(timedelta(minutes=1)))
        session = _session(_resp({"error": "invalid_grant"}, status=400))
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(GraphReauthRequired):
            await provider.headers("u1")
```

- [ ] **Step 2:** `pytest tests/unit/adapters/test_microsoft_graph_auth.py -v` → FAIL.

- [ ] **Step 3: Implement**

```python
"""
MicrosoftGraphTokenProvider — bearer headers for Microsoft Graph (docs/10_rfcs/USER_DRIVE_RFC.md §4.2).

Shared by every Microsoft Graph adapter (To Do, the user drive). One credentials record per
`provider` key with its own scope, so integrations are revoked independently. The access
token is cached in memory for at most 5 minutes: one credentials-store read per 5 minutes,
not per HTTP call, and a disconnect or reconnect reaches every instance within that time.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import aiohttp  # module import on purpose: tests patch aiohttp.ClientSession

from ..domain.email import OAuthCredentials
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..utils.logger import logger

_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
_REFRESH_MARGIN = timedelta(minutes=5)
CACHE_TTL = timedelta(minutes=5)  # owner decision (USER_DRIVE_RFC §4.2): disconnect/reconnect seen within 5 min


class GraphReauthRequired(ValueError):
    """The refresh token is no longer accepted (expired, revoked): the user must reconnect."""


class MicrosoftGraphTokenProvider:
    def __init__(self, oauth: OAuthCredentialsPort, client_id: str, client_secret: str,
                 provider: str, scope: str) -> None:
        self._oauth = oauth
        self._client_id = client_id
        self._client_secret = client_secret
        self._provider = provider
        self._scope = scope
        # user_id → (access_token, token_expiry, fetched_at)
        self._cache: Dict[str, Tuple[str, datetime, datetime]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def invalidate(self, user_id: str) -> None:
        self._cache.pop(user_id, None)

    def _fresh(self, user_id: str) -> Optional[str]:
        cached = self._cache.get(user_id)
        now = datetime.now(timezone.utc)
        if cached and cached[1] > now + _REFRESH_MARGIN and cached[2] > now - CACHE_TTL:
            return cached[0]
        return None

    async def headers(self, user_id: str, *, force_refresh: bool = False) -> Optional[Dict[str, str]]:
        """Authorization header, or None when the user has not connected this provider."""
        token = None if force_refresh else self._fresh(user_id)
        if token:
            return {"Authorization": f"Bearer {token}"}
        async with self._locks.setdefault(user_id, asyncio.Lock()):
            token = None if force_refresh else self._fresh(user_id)
            if token:
                return {"Authorization": f"Bearer {token}"}
            creds = await self._oauth.get_credentials(user_id, self._provider)
            if creds is None:
                self._cache.pop(user_id, None)
                return None
            if force_refresh or creds.token_expiry <= datetime.now(timezone.utc) + _REFRESH_MARGIN:
                creds = await self.refresh(creds)
            self._cache[user_id] = (creds.access_token, creds.token_expiry, datetime.now(timezone.utc))
            return {"Authorization": f"Bearer {creds.access_token}"}

    async def refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        """Exchange the refresh token for a new access token and persist it."""
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
            "scope": self._scope,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(_TOKEN_URL, data=data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"MS token refresh failed ({self._provider}, {resp.status}): {body[:300]}")
                    if "invalid_grant" in body:
                        raise GraphReauthRequired(f"MS token refresh rejected ({self._provider}): reconnect needed")
                    raise ValueError(f"MS token refresh failed ({resp.status}): {body}")
                payload = await resp.json()

        new_creds = OAuthCredentials(
            user_id=creds.user_id,
            provider=self._provider,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", creds.refresh_token),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=payload.get("expires_in", 3600)),
            scopes=creds.scopes,
            email_address=creds.email_address,
        )
        await self._oauth.save_credentials(new_creds)
        logger.info(f"🔄 MS token refreshed ({self._provider}) for user {creds.user_id[:8]}")
        return new_creds
```

- [ ] **Step 4: To Do adapter delegates to it**

In `src/adapters/microsoft_todo_adapter.py`: `from .microsoft_graph_auth import MicrosoftGraphTokenProvider`; at the end of `__init__`:

```python
        self._tokens = MicrosoftGraphTokenProvider(
            oauth_credentials, client_id, client_secret, _PROVIDER, "Tasks.ReadWrite offline_access",
        )
```

Replace `_get_headers` and `_refresh_token` bodies:

```python
    async def _get_headers(self, user_id: str) -> Dict[str, str]:
        """Return Authorization headers with a valid (refreshed if needed) access token."""
        headers = await self._tokens.headers(user_id)
        if headers is None:
            raise ValueError(f"No MS To Do credentials for user {user_id[:8]}")
        return headers

    async def _refresh_token(self, creds: OAuthCredentials) -> OAuthCredentials:
        """Exchange refresh_token → new access_token and persist."""
        return await self._tokens.refresh(creds)
```

Remove `_TOKEN_URL` from the To Do module only if nothing else there uses it (`grep -n _TOKEN_URL src/adapters/microsoft_todo_adapter.py`).

- [ ] **Step 5:** `pytest tests/unit/adapters/test_microsoft_graph_auth.py tests/unit/adapters/test_microsoft_todo_adapter.py -v` → all PASS. A To Do failure (e.g. a test that counts credential reads per call) → do not edit it; hand it to the reviewer with assertion + actual vs expected. Behaviour change to note in the commit body: a To Do disconnect now reaches other instances within 5 minutes instead of immediately (owner decision, RFC §4.2).

- [ ] **Step 6: Commit** — `git add src/adapters/microsoft_graph_auth.py src/adapters/microsoft_todo_adapter.py tests/unit/adapters/test_microsoft_graph_auth.py && git commit -m "refactor(ms-graph): shared token provider with in-memory cache and reauth signal"`

---

## Task 4: `OneDriveAdapter`

**Files:**
- Create: `src/adapters/onedrive_adapter.py`
- Test: `tests/unit/adapters/test_onedrive_adapter.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: `OneDriveAdapter(oauth_credentials, client_id, client_secret)`; module constant `ONEDRIVE_PROVIDER = "microsoft_onedrive"`.

Transport rules (§4.2, §4.3): one `aiohttp.ClientSession` per port call; token from the provider cache; `401` → invalidate + one forced refresh + retry, a second `401` → `DriveNotConnectedError`; `GraphReauthRequired` → `DriveNotConnectedError`; `429`/`503` → up to 3 retries honouring `Retry-After` (cap 30 s); `404` → `DriveItemNotFoundError`; `409` → `DriveNameConflictError`. Paths: both sides decoded with `unquote`, made relative to the area root; a prefix mismatch logs a warning and re-reads the item once, then labels it `…/<name>` (never silently top level). The root JSON is cached per user for 300 s.

Call order inside a port method (the mocks below follow it): the provider call first, then `_item()` reads the root (`GET /me/drive/special/approot`, once per adapter instance thanks to the cache), then — only on a missing/mismatched parent path — one more `GET` of the item.

- [ ] **Step 1: Failing wire tests**

```python
"""
Wire tests for OneDriveAdapter. Mock boundary: aiohttp.ClientSession (HTTP layer).
Never mock at UserDrivePort level (docs/how_to/ADAPTER_WIRE_TESTING.md).
Fixtures use percent-encoded parent paths, as Graph returns them (itemReference.path).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest

from src.adapters.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.email import OAuthCredentials
from src.domain.user_drive import DriveItemNotFoundError, DriveNameConflictError, DriveNotConnectedError
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.ports.user_drive_port import UserDrivePort

_ROOT = {"id": "root1", "name": "Alek-bot", "folder": {"childCount": 1},
         "parentReference": {"path": "/drive/root:/Apps"}}
_ENC_PARENT = "/drive/root:/Apps/Alek-bot/" + quote("Встречи 2026")
_FILE = {"id": "f1", "name": "заметка.txt", "size": 12, "file": {"mimeType": "text/plain"},
         "parentReference": {"path": _ENC_PARENT}, "@microsoft.graph.downloadUrl": "https://dl.example/a"}


def _creds():
    return OAuthCredentials(user_id="u1", provider=ONEDRIVE_PROVIDER, access_token="tok", refresh_token="r",
                            token_expiry=datetime.now(timezone.utc) + timedelta(hours=1), scopes=[],
                            email_address="")


def _resp(json_data=None, status=200, body=b"", headers=None):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.headers = headers or {}
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=str(json_data))
    r.read = AsyncMock(return_value=body)
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(**methods):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    for name, value in methods.items():
        target = getattr(s, name)
        if isinstance(value, list):
            target.side_effect = value
        else:
            target.return_value = value
    return s


def _adapter(creds="default"):
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get_credentials.return_value = _creds() if creds == "default" else creds
    oauth.is_connected.return_value = creds is not None
    return OneDriveAdapter(oauth, "cid", "csecret"), oauth


class TestPortCompliance:
    def test_is_user_drive_port(self):
        assert issubclass(OneDriveAdapter, UserDrivePort)

    def test_provider_key(self):
        assert ONEDRIVE_PROVIDER == "microsoft_onedrive"


class TestPaths:
    async def test_percent_encoded_path_is_readable_and_relative(self):
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp(_FILE), _resp(_ROOT)])):
            item = await adapter.get_item("u1", "f1")
        assert item.path == "Встречи 2026/заметка.txt"
        assert item.mime_type == "text/plain" and item.size_bytes == 12

    async def test_root_has_empty_path(self):
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=_resp(_ROOT))):
            root = await adapter.get_root("u1")
        assert root.path == "" and root.is_folder and root.item_id == "root1"

    async def test_prefix_mismatch_rereads_then_marks_unknown(self):
        odd = {**_FILE, "parentReference": {"path": "/drive/root:/Elsewhere"}}
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp(odd), _resp(_ROOT), _resp(odd)])):
            item = await adapter.get_item("u1", "f1")
        assert item.path == "…/заметка.txt"

    async def test_search_hit_without_parent_path_is_reread(self):
        hit = {"id": "f1", "name": "заметка.txt", "file": {}, "parentReference": {}}
        adapter, _ = _adapter()
        session = _session(get=[_resp({"value": [hit]}), _resp(_ROOT), _resp(_FILE)])
        with patch("aiohttp.ClientSession", return_value=session):
            items = await adapter.search("u1", "заметка", limit=10)
        assert [i.path for i in items] == ["Встречи 2026/заметка.txt"]
        assert "/me/drive/special/approot/search(q=" in session.get.call_args_list[0].args[0]


class TestAuthAndErrors:
    async def test_no_credentials_raises_not_connected(self):
        adapter, _ = _adapter(creds=None)
        with pytest.raises(DriveNotConnectedError):
            await adapter.get_root("u1")

    async def test_401_after_forced_refresh_is_not_connected(self):
        adapter, _ = _adapter()
        adapter._tokens.headers = AsyncMock(return_value={"Authorization": "Bearer t"})
        session = _session(get=[_resp({}, status=401), _resp({}, status=401)])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveNotConnectedError):
            await adapter.get_root("u1")
        assert {"force_refresh": True} in [c.kwargs for c in adapter._tokens.headers.await_args_list]

    async def test_401_refresh_still_happens_after_throttling(self):
        adapter, _ = _adapter()
        adapter._tokens.headers = AsyncMock(return_value={"Authorization": "Bearer t"})
        throttled = [_resp({}, status=429, headers={"Retry-After": "0"}) for _ in range(3)]
        session = _session(get=[*throttled, _resp({}, status=401), _resp(_ROOT)])
        with patch("aiohttp.ClientSession", return_value=session):
            root = await adapter.get_root("u1")
        assert root.item_id == "root1"
        assert {"force_refresh": True} in [c.kwargs for c in adapter._tokens.headers.await_args_list]

    async def test_429_retried_with_retry_after(self):
        adapter, _ = _adapter()
        session = _session(get=[_resp({}, status=429, headers={"Retry-After": "0"}), _resp(_ROOT)])
        with patch("aiohttp.ClientSession", return_value=session):
            root = await adapter.get_root("u1")
        assert root.item_id == "root1"

    def test_retry_after_http_date_falls_back_to_backoff(self):
        from src.adapters.onedrive_adapter import _retry_after
        assert _retry_after("Wed, 21 Oct 2026 07:28:00 GMT", 1) == 2.0
        assert _retry_after("120", 0) == 30.0

    async def test_404_raises_not_found(self):
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp({}, status=404)])), \
                pytest.raises(DriveItemNotFoundError):
            await adapter.get_item("u1", "gone")

    async def test_409_on_create_folder_raises_conflict(self):
        adapter, _ = _adapter()
        session = _session(post=_resp({}, status=409))
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveNameConflictError):
            await adapter.create_folder("u1", "root1", "Inbox")

    async def test_one_session_per_call(self):
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp(_FILE), _resp(_ROOT)])) as cs:
            await adapter.get_item("u1", "f1")
        assert cs.call_count == 1


class TestUpload:
    async def test_upload_uses_conflict_rename(self):
        # Spike A8 recorded the provider's clash suffix format — note it here.
        adapter, _ = _adapter()
        session = _session(put=_resp(_FILE, status=201), get=_resp(_ROOT))
        with patch("aiohttp.ClientSession", return_value=session):
            item = await adapter.upload("u1", "p1", "заметка.txt", b"hello", "text/plain")
        url = session.put.call_args.args[0]
        assert f"/me/drive/items/p1:/{quote('заметка.txt')}:/content" in url
        assert "conflictBehavior=rename" in url
        assert item.item_id == "f1"

    async def test_large_upload_uses_session_without_auth_header(self):
        adapter, _ = _adapter()
        data = b"0" * (6 * 1024 * 1024)
        session = _session(post=_resp({"uploadUrl": "https://up.example/s"}),
                           put=[_resp({}, status=202), _resp(_FILE, status=201)], get=_resp(_ROOT))
        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.upload("u1", "p1", "big.bin", data, "application/octet-stream")
        assert "createUploadSession" in session.post.call_args.args[0]
        first = session.put.call_args_list[0]
        assert first.args[0] == "https://up.example/s"
        assert "Authorization" not in first.kwargs["headers"]
        assert first.kwargs["headers"]["Content-Range"] == f"bytes 0-{5 * 1024 * 1024 - 1}/{len(data)}"


class TestDownloadAndMutations:
    async def test_download_via_download_url_without_auth(self):
        adapter, _ = _adapter()
        session = _session(get=[_resp(_FILE), _resp(body=b"hello")])
        with patch("aiohttp.ClientSession", return_value=session):
            assert await adapter.download("u1", "f1") == b"hello"
        last = session.get.call_args_list[-1]
        assert last.args[0] == "https://dl.example/a" and "headers" not in last.kwargs

    async def test_move_patches_parent_and_name(self):
        adapter, _ = _adapter()
        session = _session(patch=_resp(_FILE), get=_resp(_ROOT))
        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.move("u1", "f1", new_parent_id="p2", new_name="b.txt")
        assert session.patch.call_args.kwargs["json"] == {"parentReference": {"id": "p2"}, "name": "b.txt"}

    async def test_delete(self):
        adapter, _ = _adapter()
        session = _session(delete=_resp(status=204))
        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.delete("u1", "f1")
        assert session.delete.call_args.args[0].endswith("/me/drive/items/f1")

    async def test_list_children_follows_next_link(self):
        adapter, _ = _adapter()
        page1 = {"value": [_FILE], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"}
        page2 = {"value": [{**_FILE, "id": "f2", "name": "b.txt"}]}
        session = _session(get=[_resp(page1), _resp(_ROOT), _resp(page2)])
        with patch("aiohttp.ClientSession", return_value=session):
            kids = await adapter.list_children("u1", "p1")
        assert [k.item_id for k in kids] == ["f1", "f2"]
```

- [ ] **Step 2:** `pytest tests/unit/adapters/test_onedrive_adapter.py -v` → FAIL.

- [ ] **Step 3: Implement**

```python
"""
OneDriveAdapter — UserDrivePort over Microsoft Graph, scoped to the app's App Folder
(docs/10_rfcs/USER_DRIVE_RFC.md §2, §4.2, §4.3).

Scope Files.ReadWrite.AppFolder: Graph itself refuses anything outside the app folder.
Ids are Graph item ids (stable across move/rename). Paths are display only: Graph returns
them percent-encoded, so they are decoded and made relative to the app folder here. The
provider name stays inside this module and the OAuth edge.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import aiohttp  # module import on purpose: tests patch aiohttp.ClientSession

from ..domain.user_drive import (
    DriveItem,
    DriveItemNotFoundError,
    DriveNameConflictError,
    DriveNotConnectedError,
    join_drive_path,
)
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.user_drive_port import UserDrivePort
from ..utils.logger import logger
from .microsoft_graph_auth import GraphReauthRequired, MicrosoftGraphTokenProvider

ONEDRIVE_PROVIDER = "microsoft_onedrive"
_SCOPE = "Files.ReadWrite.AppFolder offline_access"
_GRAPH = "https://graph.microsoft.com/v1.0"
_SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
_CHUNK = 16 * 320 * 1024  # 5 MiB; Graph requires multiples of 320 KiB
_ROOT_TTL_S = 300.0
_RETRIES = 3
_MAX_RETRY_AFTER_S = 30.0
_UNKNOWN_PARENT = "…"


def _retry_after(value: Optional[str], attempt: int) -> float:
    """Seconds to wait: Retry-After in seconds if parseable (it may be an HTTP date), else backoff."""
    try:
        seconds = float(value) if value is not None else float(2 ** attempt)
    except ValueError:
        seconds = float(2 ** attempt)
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_S)


class OneDriveAdapter(UserDrivePort):
    def __init__(self, oauth_credentials: OAuthCredentialsPort, client_id: str, client_secret: str) -> None:
        self._oauth = oauth_credentials
        self._tokens = MicrosoftGraphTokenProvider(oauth_credentials, client_id, client_secret,
                                                   ONEDRIVE_PROVIDER, _SCOPE)
        # user_id → (fetched_at, root json); the owner may rename/move the folder, hence a TTL.
        self._root_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        logger.info("✅ OneDriveAdapter initialized")

    # -- connection ---------------------------------------------------------

    @property
    def display_name(self) -> str:
        return "OneDrive"

    async def is_connected(self, user_id: str) -> bool:
        return await self._oauth.is_connected(user_id, ONEDRIVE_PROVIDER)

    async def disconnect(self, user_id: str) -> None:
        await self._oauth.revoke_credentials(user_id, ONEDRIVE_PROVIDER)
        self._tokens.invalidate(user_id)
        self._root_cache.pop(user_id, None)

    # -- reads --------------------------------------------------------------

    async def get_root(self, user_id: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            return self._to_item(await self._root_raw(s, user_id), "", is_root=True)

    async def get_item(self, user_id: str, item_id: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, user_id, "GET", f"/me/drive/items/{quote(item_id)}")
            return await self._item(s, user_id, raw)

    async def list_children(self, user_id: str, folder_id: str) -> List[DriveItem]:
        async with aiohttp.ClientSession() as s:
            url: Optional[str] = f"/me/drive/items/{quote(folder_id)}/children?$top=200"
            items: List[DriveItem] = []
            while url:
                page = await self._call(s, user_id, "GET", url)
                for raw in page.get("value", []):
                    items.append(await self._item(s, user_id, raw))
                url = page.get("@odata.nextLink")
            return items

    async def search(self, user_id: str, query: str, limit: int) -> List[DriveItem]:
        async with aiohttp.ClientSession() as s:
            escaped = query.replace("'", "''")
            page = await self._call(s, user_id, "GET",
                                    f"/me/drive/special/approot/search(q='{quote(escaped)}')?$top={limit}")
            return [await self._item(s, user_id, raw) for raw in page.get("value", [])[:limit]]

    async def download(self, user_id: str, item_id: str) -> bytes:
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, user_id, "GET", f"/me/drive/items/{quote(item_id)}")
            url = raw.get("@microsoft.graph.downloadUrl")
            if not url:
                raise DriveItemNotFoundError(f"No downloadable content for item {item_id}")
            # Pre-authenticated short-lived URL on another host: never send the bearer token there.
            async with s.get(url) as resp:
                if resp.status != 200:
                    raise ValueError(f"Drive download failed ({resp.status})")
                return bytes(await resp.read())

    # -- writes -------------------------------------------------------------

    async def upload(self, user_id: str, parent_id: str, filename: str, data: bytes, content_type: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            target = f"/me/drive/items/{quote(parent_id)}:/{quote(filename)}:"
            if len(data) <= _SIMPLE_UPLOAD_MAX:
                raw = await self._call(s, user_id, "PUT",
                                       f"{target}/content?@microsoft.graph.conflictBehavior=rename",
                                       data=data, content_type=content_type)
            else:
                raw = await self._session_upload(s, user_id, f"{target}/createUploadSession",
                                                 {"item": {"@microsoft.graph.conflictBehavior": "rename"}}, data)
            return await self._item(s, user_id, raw)

    async def replace_content(self, user_id: str, item_id: str, data: bytes, content_type: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            base = f"/me/drive/items/{quote(item_id)}"
            if len(data) <= _SIMPLE_UPLOAD_MAX:
                raw = await self._call(s, user_id, "PUT", f"{base}/content", data=data, content_type=content_type)
            else:
                raw = await self._session_upload(s, user_id, f"{base}/createUploadSession",
                                                 {"item": {"@microsoft.graph.conflictBehavior": "replace"}}, data)
            return await self._item(s, user_id, raw)

    async def move(self, user_id: str, item_id: str, new_parent_id: Optional[str] = None,
                   new_name: Optional[str] = None) -> DriveItem:
        body: Dict[str, Any] = {}
        if new_parent_id:
            body["parentReference"] = {"id": new_parent_id}
        if new_name:
            body["name"] = new_name
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, user_id, "PATCH", f"/me/drive/items/{quote(item_id)}", json=body)
            return await self._item(s, user_id, raw)

    async def create_folder(self, user_id: str, parent_id: str, name: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, user_id, "POST", f"/me/drive/items/{quote(parent_id)}/children",
                                   json={"name": name, "folder": {},
                                         "@microsoft.graph.conflictBehavior": "fail"})
            return await self._item(s, user_id, raw)

    async def delete(self, user_id: str, item_id: str) -> None:
        async with aiohttp.ClientSession() as s:
            await self._call(s, user_id, "DELETE", f"/me/drive/items/{quote(item_id)}")

    # -- transport ----------------------------------------------------------

    async def _headers(self, user_id: str, *, force_refresh: bool = False) -> Dict[str, str]:
        try:
            headers = await self._tokens.headers(user_id, force_refresh=force_refresh)
        except GraphReauthRequired as exc:
            raise DriveNotConnectedError("Drive access expired; reconnect needed") from exc
        if headers is None:
            raise DriveNotConnectedError(f"No drive credentials for user {user_id[:8]}")
        return headers

    async def _call(self, s: aiohttp.ClientSession, user_id: str, method: str, path: str, *,
                    json: Any = None, data: Optional[bytes] = None,
                    content_type: Optional[str] = None) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{_GRAPH}{path}"
        throttled = 0          # 429/503 retries — separate from the 401 refresh,
        refreshed = False      # so throttling never consumes the forced refresh (§4.2)
        while True:
            headers = await self._headers(user_id)
            if content_type:
                headers = {**headers, "Content-Type": content_type}
            kwargs: Dict[str, Any] = {"headers": headers}
            if json is not None:
                kwargs["json"] = json
            if data is not None:
                kwargs["data"] = data
            async with getattr(s, method.lower())(url, **kwargs) as resp:
                status = resp.status
                if status == 401:
                    if refreshed:
                        raise DriveNotConnectedError("Drive access rejected after refresh; reconnect needed")
                    refreshed = True
                    self._tokens.invalidate(user_id)
                    await self._headers(user_id, force_refresh=True)  # next attempt uses the fresh token
                    continue
                if status in (429, 503) and throttled < _RETRIES:
                    delay = _retry_after(resp.headers.get("Retry-After"), throttled)
                    throttled += 1
                    logger.warning(f"Drive {method} throttled ({status}); retrying in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    continue
                if status == 404:
                    raise DriveItemNotFoundError(f"Drive item not found: {path}")
                if status == 409:
                    raise DriveNameConflictError(f"Drive name taken: {path}")
                if status == 204:
                    return {}
                if not resp.ok:
                    body = await resp.text()
                    logger.error(f"Drive {method} {path} failed ({status}): {body[:300]}")
                    raise ValueError(f"Drive {method} failed ({status})")
                return dict(await resp.json())

    async def _session_upload(self, s: aiohttp.ClientSession, user_id: str, path: str,
                              body: Dict[str, Any], data: bytes) -> Dict[str, Any]:
        upload_url = (await self._call(s, user_id, "POST", path, json=body))["uploadUrl"]
        total = len(data)
        result: Dict[str, Any] = {}
        for start in range(0, total, _CHUNK):
            chunk = data[start:start + _CHUNK]
            end = start + len(chunk) - 1
            # The upload URL is pre-authenticated; a bearer header here is rejected.
            headers = {"Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{end}/{total}"}
            async with s.put(upload_url, headers=headers, data=chunk) as resp:
                if resp.status not in (200, 201, 202):
                    text = await resp.text()
                    logger.error(f"Drive chunk upload failed ({resp.status}): {text[:300]}")
                    raise ValueError(f"Drive chunk upload failed ({resp.status})")
                if resp.status in (200, 201):
                    result = dict(await resp.json())
        return result

    # -- paths --------------------------------------------------------------

    async def _root_raw(self, s: aiohttp.ClientSession, user_id: str) -> Dict[str, Any]:
        cached = self._root_cache.get(user_id)
        if cached and time.monotonic() - cached[0] < _ROOT_TTL_S:
            return cached[1]
        raw = await self._call(s, user_id, "GET", "/me/drive/special/approot")
        self._root_cache[user_id] = (time.monotonic(), raw)
        return raw

    async def _root_abs(self, s: aiohttp.ClientSession, user_id: str) -> str:
        """Decoded absolute path of the app folder, e.g. '/drive/root:/Apps/Alek-bot'."""
        raw = await self._root_raw(s, user_id)
        parent = unquote((raw.get("parentReference") or {}).get("path", "") or "")
        return f"{parent}/{raw.get('name', '')}"

    @staticmethod
    def _rel_parent(raw: Dict[str, Any], root_abs: str) -> Optional[str]:
        """Parent path relative to the area root, decoded; None if absent or not under the root."""
        encoded = (raw.get("parentReference") or {}).get("path")
        if not encoded:
            return None
        parent = unquote(encoded)
        if parent == root_abs:
            return ""
        if parent.startswith(root_abs + "/"):
            return parent[len(root_abs) + 1:]
        return None

    async def _item(self, s: aiohttp.ClientSession, user_id: str, raw: Dict[str, Any]) -> DriveItem:
        root_abs = await self._root_abs(s, user_id)
        rel = self._rel_parent(raw, root_abs)
        if rel is None and raw.get("id"):
            if (raw.get("parentReference") or {}).get("path"):
                logger.warning(f"Drive path outside the app folder for item {raw['id']}; re-reading")
            raw = await self._call(s, user_id, "GET", f"/me/drive/items/{quote(str(raw['id']))}")
            rel = self._rel_parent(raw, root_abs)
            if rel is None:
                logger.warning(f"Drive path still unresolved for item {raw.get('id')}")
                rel = _UNKNOWN_PARENT
        return self._to_item(raw, rel or "")

    @staticmethod
    def _to_item(raw: Dict[str, Any], parent_rel: str, *, is_root: bool = False) -> DriveItem:
        name = str(raw.get("name", ""))
        modified = raw.get("lastModifiedDateTime")
        return DriveItem(
            item_id=str(raw.get("id", "")),
            name=name,
            path="" if is_root else join_drive_path(parent_rel, name),
            is_folder="folder" in raw,
            size_bytes=int(raw.get("size") or 0),
            mime_type=str((raw.get("file") or {}).get("mimeType", "")),
            modified_at=datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None,
            web_url=str(raw.get("webUrl", "")),
            child_count=int((raw.get("folder") or {}).get("childCount") or 0),
        )
```

- [ ] **Step 4:** `pytest tests/unit/adapters/test_onedrive_adapter.py -v && ruff check src/adapters/onedrive_adapter.py src/adapters/microsoft_graph_auth.py && make check-types` → PASS; `check-types` shows no new errors for the two new files (compare counts before/after).

- [ ] **Step 5: Commit** — `git add src/adapters/onedrive_adapter.py tests/unit/adapters/test_onedrive_adapter.py && git commit -m "feat(drive): OneDriveAdapter — App Folder, decoded paths, retries, reauth"`

---

## Task 5: `UserDriveService`

**Files:**
- Create: `src/services/user_drive_service.py`
- Test: `tests/unit/services/test_user_drive_service.py`

**Interfaces:**
- Consumes: `UserDrivePort`, domain outcomes and errors (Tasks 1–2).
- Produces (all `async` except the property): `display_name`; `resolve_folder(user_id, path, *, create) -> FolderResolution`; `ensure_folder(user_id, folder) -> FolderResolution`; `save(user_id, data, filename, content_type, folder) -> SaveOutcome`; `list_folder(user_id, folder) -> ListOutcome`; `search(user_id, query) -> List[DriveItem]`; `get_item(user_id, item_id) -> DriveItem`; `move(user_id, item_id, folder, new_name) -> MoveOutcome`; `append_text(user_id, item_id, text) -> UpdateOutcome`; `replace_with(user_id, item_id, data, content_type) -> UpdateOutcome`; `delete(user_id, item_id) -> DeleteOutcome`. Constants `LIST_LIMIT = 100`, `SEARCH_LIMIT = 25`, `COUNT_CAP = 10_000`.

- [ ] **Step 1: Failing tests**

```python
"""UserDriveService rules (docs/10_rfcs/USER_DRIVE_RFC.md §4.7–§4.11)."""
from unittest.mock import AsyncMock

import pytest

from src.domain.user_drive import DriveItem, DriveNameConflictError, DrivePathError, DriveRootProtectedError
from src.ports.user_drive_port import UserDrivePort
from src.services.user_drive_service import LIST_LIMIT, UserDriveService

ROOT = DriveItem(item_id="root", name="Alek-bot", path="", is_folder=True)
INBOX = DriveItem(item_id="inbox", name="Inbox", path="Inbox", is_folder=True)


def _folder(item_id, name, parent=""):
    return DriveItem(item_id=item_id, name=name, path=f"{parent}/{name}" if parent else name, is_folder=True)


def _file(item_id, name, parent="", size=10, mime="text/plain"):
    return DriveItem(item_id=item_id, name=name, path=f"{parent}/{name}" if parent else name,
                     is_folder=False, size_bytes=size, mime_type=mime)


@pytest.fixture
def drive():
    d = AsyncMock(spec=UserDrivePort)
    d.get_root.return_value = ROOT
    return d


@pytest.fixture
def service(drive):
    return UserDriveService(drive)


class TestResolveFolder:
    async def test_existing_case_insensitive_no_create(self, service, drive):
        drive.list_children.side_effect = [[_folder("m", "Встречи")], [_folder("y", "2026", "Встречи")]]
        res = await service.resolve_folder("u1", "встречи/2026", create=True)
        assert res.folder.item_id == "y" and res.created == []
        drive.create_folder.assert_not_called()

    async def test_missing_segments_created(self, service, drive):
        drive.list_children.side_effect = [[_folder("m", "Встречи")], []]
        drive.create_folder.return_value = _folder("n", "2026", "Встречи")
        res = await service.resolve_folder("u1", "Встречи/2026", create=True)
        drive.create_folder.assert_awaited_once_with("u1", "m", "2026")
        assert res.created == ["Встречи/2026"]

    async def test_create_race_uses_winner(self, service, drive):
        drive.list_children.side_effect = [[], [INBOX]]
        drive.create_folder.side_effect = DriveNameConflictError("taken")
        res = await service.resolve_folder("u1", "Inbox", create=True)
        assert res.folder is INBOX and res.created == []

    async def test_blank_is_root_and_escape_rejected(self, service):
        assert (await service.resolve_folder("u1", "  ", create=False)).folder is ROOT
        with pytest.raises(DrivePathError):
            await service.resolve_folder("u1", "../x", create=True)


class TestSaveDuplicates:
    async def test_default_inbox_new_file(self, service, drive):
        drive.list_children.side_effect = [[], []]
        drive.create_folder.return_value = INBOX
        drive.upload.return_value = _file("f", "a.pdf", "Inbox")
        out = await service.save("u1", b"x", "a.pdf", "application/pdf", None)
        drive.upload.assert_awaited_once_with("u1", "inbox", "a.pdf", b"x", "application/pdf")
        assert out.created == ["Inbox"] and not out.already_existed and not out.renamed

    async def test_same_name_same_bytes_not_uploaded(self, service, drive):
        existing = _file("e", "a.pdf", "Inbox", size=3)
        drive.list_children.side_effect = [[INBOX], [existing]]
        drive.download.return_value = b"abc"
        out = await service.save("u1", b"abc", "a.pdf", "application/pdf", None)
        assert out.already_existed and out.item is existing
        drive.upload.assert_not_called()

    async def test_nfd_existing_name_is_the_same_file(self, service, drive):
        import unicodedata
        existing = _file("e", unicodedata.normalize("NFD", "Отчёт.pdf"), "Inbox", size=3)
        drive.list_children.side_effect = [[INBOX], [existing]]
        drive.download.return_value = b"abc"
        out = await service.save("u1", b"abc", "Отчёт.pdf", "application/pdf", None)
        assert out.already_existed
        drive.upload.assert_not_called()

    async def test_same_name_different_size_uploaded_renamed(self, service, drive):
        drive.list_children.side_effect = [[INBOX], [_file("e", "a.pdf", "Inbox", size=99)]]
        drive.upload.return_value = _file("n", "a 1.pdf", "Inbox")  # suffix format per spike A8
        out = await service.save("u1", b"abc", "a.pdf", "application/pdf", None)
        drive.download.assert_not_called()
        assert out.renamed and out.item.name == "a 1.pdf"


class TestUpdate:
    async def test_append_adds_line(self, service, drive):
        note = _file("n", "todo.md", "Notes", size=5, mime="text/markdown")
        drive.get_item.return_value = note
        drive.download.return_value = b"- one"
        drive.replace_content.return_value = note
        out = await service.append_text("u1", "n", "- two")
        drive.replace_content.assert_awaited_once_with("u1", "n", b"- one\n- two\n", "text/markdown")
        assert out.mode == "append" and out.before_size == 5

    async def test_append_refuses_binary(self, service, drive):
        drive.get_item.return_value = _file("p", "a.pdf", size=5, mime="application/pdf")
        with pytest.raises(DrivePathError):
            await service.append_text("u1", "p", "x")

    async def test_replace_refuses_folder(self, service, drive):
        drive.get_item.return_value = _folder("m", "Встречи")
        with pytest.raises(DrivePathError):
            await service.replace_with("u1", "m", b"x", "text/plain")


class TestDeleteMove:
    async def test_delete_root_refused(self, service, drive):
        with pytest.raises(DriveRootProtectedError):
            await service.delete("u1", "root")
        drive.delete.assert_not_called()

    async def test_folder_count_walks_subtree(self, service, drive):
        top = _folder("t", "Встречи")
        drive.get_item.return_value = top
        drive.list_children.side_effect = [[_folder("s", "2025", "Встречи"), _file("a", "a")],
                                           [_file("b", "b"), _file("c", "c")]]
        out = await service.delete("u1", "t")
        assert out.file_count == 3 and out.item is top
        drive.delete.assert_awaited_once_with("u1", "t")

    async def test_move_root_refused(self, service):
        with pytest.raises(DriveRootProtectedError):
            await service.move("u1", "root", "X", None)

    async def test_rename_only(self, service, drive):
        before = _file("a", "a.txt", "Inbox")
        drive.get_item.return_value = before
        drive.move.return_value = _file("a", "b.txt", "Inbox")
        out = await service.move("u1", "a", None, "b.txt")
        drive.move.assert_awaited_once_with("u1", "a", new_parent_id=None, new_name="b.txt")
        assert out.before is before and out.after.path == "Inbox/b.txt"

    async def test_move_needs_folder_or_name(self, service):
        with pytest.raises(DrivePathError):
            await service.move("u1", "a", None, None)


async def test_list_sorted_and_truncated(service, drive):
    drive.list_children.return_value = [_file(str(i), f"f{i:03}") for i in range(LIST_LIMIT + 5)] + [_folder("z", "Zeta")]
    out = await service.list_folder("u1", None)
    assert out.items[0].name == "Zeta" and len(out.items) == LIST_LIMIT and out.truncated
```

- [ ] **Step 2:** `pytest tests/unit/services/test_user_drive_service.py -v` → FAIL.

- [ ] **Step 3: Implement**

```python
"""
UserDriveService — rules over UserDrivePort (docs/10_rfcs/USER_DRIVE_RFC.md §4.7–§4.11).

Deterministic, no LLM: destination folders resolved segment by segment (case-insensitive,
missing segments created, a create race resolved by re-listing); a save never overwrites
and never duplicates identical content; append keeps everything that was there; the area
root is never deleted or moved; a folder's file count is taken before it is deleted.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..domain.user_drive import (
    DEFAULT_INBOX_FOLDER,
    MAX_DRIVE_APPEND_FILE_BYTES,
    MAX_DRIVE_DOWNLOAD_BYTES,
    DeleteOutcome,
    DriveItem,
    DriveNameConflictError,
    DrivePathError,
    DriveRootProtectedError,
    FolderResolution,
    ListOutcome,
    MoveOutcome,
    SaveOutcome,
    UpdateOutcome,
    format_size,
    is_text_file,
    join_drive_path,
    match_child_folder,
    name_key,
    split_folder_path,
)
from ..ports.user_drive_port import UserDrivePort

LIST_LIMIT = 100
SEARCH_LIMIT = 25
COUNT_CAP = 10_000


class UserDriveService:
    def __init__(self, drive: UserDrivePort) -> None:
        self._drive = drive

    @property
    def display_name(self) -> str:
        return self._drive.display_name

    async def resolve_folder(self, user_id: str, path: Optional[str], *, create: bool) -> FolderResolution:
        segments = split_folder_path(path or "")
        current = await self._drive.get_root(user_id)
        created: List[str] = []
        for segment in segments:
            match = match_child_folder(await self._drive.list_children(user_id, current.item_id), segment)
            if match is None:
                if not create:
                    raise DrivePathError(f"No folder '{join_drive_path(current.path, segment)}' on the drive")
                try:
                    match = await self._drive.create_folder(user_id, current.item_id, segment)
                    created.append(match.path or join_drive_path(current.path, segment))
                except DriveNameConflictError:
                    # A parallel save created it first (§4.7): use the winner.
                    match = match_child_folder(await self._drive.list_children(user_id, current.item_id), segment)
                    if match is None:
                        raise
            current = match
        return FolderResolution(folder=current, created=created)

    async def ensure_folder(self, user_id: str, folder: str) -> FolderResolution:
        if not split_folder_path(folder):
            raise DrivePathError("A folder name is required")
        return await self.resolve_folder(user_id, folder, create=True)

    async def save(self, user_id: str, data: bytes, filename: str, content_type: str,
                   folder: Optional[str]) -> SaveOutcome:
        target = folder if folder and folder.strip() else DEFAULT_INBOX_FOLDER
        resolution = await self.resolve_folder(user_id, target, create=True)
        children = await self._drive.list_children(user_id, resolution.folder.item_id)
        wanted = name_key(filename)
        same_name = next((c for c in children if not c.is_folder and name_key(c.name) == wanted), None)
        if (same_name is not None and same_name.size_bytes == len(data)
                and len(data) <= MAX_DRIVE_DOWNLOAD_BYTES
                and await self._drive.download(user_id, same_name.item_id) == data):
            return SaveOutcome(item=same_name, created=resolution.created, already_existed=True)
        item = await self._drive.upload(user_id, resolution.folder.item_id, filename, data, content_type)
        return SaveOutcome(item=item, created=resolution.created, renamed=name_key(item.name) != wanted)

    async def list_folder(self, user_id: str, folder: Optional[str]) -> ListOutcome:
        resolution = await self.resolve_folder(user_id, folder, create=False)
        children = await self._drive.list_children(user_id, resolution.folder.item_id)
        ordered = sorted(children, key=lambda i: (not i.is_folder, i.name.casefold()))
        return ListOutcome(folder=resolution.folder, items=ordered[:LIST_LIMIT], truncated=len(ordered) > LIST_LIMIT)

    async def search(self, user_id: str, query: str) -> List[DriveItem]:
        return await self._drive.search(user_id, query, SEARCH_LIMIT)

    async def get_item(self, user_id: str, item_id: str) -> DriveItem:
        return await self._drive.get_item(user_id, item_id)

    async def move(self, user_id: str, item_id: str, folder: Optional[str], new_name: Optional[str]) -> MoveOutcome:
        if not (folder and folder.strip()) and not (new_name and new_name.strip()):
            raise DrivePathError("Give a destination folder, a new name, or both")
        await self._refuse_root(user_id, item_id)
        before = await self._drive.get_item(user_id, item_id)
        created: List[str] = []
        parent_id: Optional[str] = None
        if folder and folder.strip():
            resolution = await self.resolve_folder(user_id, folder, create=True)
            parent_id, created = resolution.folder.item_id, resolution.created
        after = await self._drive.move(user_id, item_id, new_parent_id=parent_id,
                                       new_name=new_name.strip() if new_name else None)
        return MoveOutcome(before=before, after=after, created=created)

    async def append_text(self, user_id: str, item_id: str, text: str) -> UpdateOutcome:
        item = await self._drive.get_item(user_id, item_id)
        if item.is_folder or not is_text_file(item.name, item.mime_type):
            raise DrivePathError(f"'{item.path}' is not a text file; text can only be appended to text files")
        if item.size_bytes > MAX_DRIVE_APPEND_FILE_BYTES:
            raise DrivePathError(f"'{item.path}' is {format_size(item.size_bytes)}; appending is limited to "
                                 f"{format_size(MAX_DRIVE_APPEND_FILE_BYTES)} files")
        old = await self._drive.download(user_id, item_id)
        try:
            old.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DrivePathError(f"'{item.path}' is not UTF-8 text") from exc
        separator = b"" if not old or old.endswith(b"\n") else b"\n"
        addition = text if text.endswith("\n") else f"{text}\n"
        updated = await self._drive.replace_content(user_id, item_id, old + separator + addition.encode("utf-8"),
                                                     item.mime_type or "text/plain")
        return UpdateOutcome(item=updated, before_size=item.size_bytes, mode="append")

    async def replace_with(self, user_id: str, item_id: str, data: bytes, content_type: str) -> UpdateOutcome:
        item = await self._drive.get_item(user_id, item_id)
        if item.is_folder:
            raise DrivePathError(f"'{item.path}' is a folder; only a file's content can be replaced")
        updated = await self._drive.replace_content(user_id, item_id, data, content_type)
        return UpdateOutcome(item=updated, before_size=item.size_bytes, mode="replace")

    async def delete(self, user_id: str, item_id: str) -> DeleteOutcome:
        await self._refuse_root(user_id, item_id)
        item = await self._drive.get_item(user_id, item_id)
        count, capped = (await self._count_files(user_id, item.item_id)) if item.is_folder else (1, False)
        await self._drive.delete(user_id, item_id)
        return DeleteOutcome(item=item, file_count=count, count_capped=capped)

    async def _refuse_root(self, user_id: str, item_id: str) -> None:
        if item_id == (await self._drive.get_root(user_id)).item_id:
            raise DriveRootProtectedError("The drive area itself cannot be deleted or moved")

    async def _count_files(self, user_id: str, folder_id: str) -> Tuple[int, bool]:
        """Files in the subtree; Graph's childCount covers direct children only (§4.10)."""
        count, pending = 0, [folder_id]
        while pending:
            for child in await self._drive.list_children(user_id, pending.pop()):
                if child.is_folder:
                    pending.append(child.item_id)
                else:
                    count += 1
                    if count >= COUNT_CAP:
                        return count, True
        return count, False
```

- [ ] **Step 4:** `pytest tests/unit/services/test_user_drive_service.py tests/unit/test_req_arch_01_hexagonal_isolation.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add src/services/user_drive_service.py tests/unit/services/test_user_drive_service.py && git commit -m "feat(drive): UserDriveService — folders, no-overwrite save, append, root protection"`

---

## Task 6: `FileConversionService` reads `drive:` refs

**Files:**
- Modify: `src/services/file_conversion_service.py`
- Test: `tests/unit/services/test_file_conversion_service_drive_refs.py`

**Interfaces — produces:** constructor kwarg `drive: Optional[UserDrivePort] = None`; `async get_drive_item(ref, user_id) -> DriveItem` (`FileNotFoundError` when no drive is wired); `resolve_content(drive_ref)` → `[File: <path>]…` or a `[System: …]` alert (folder / over text cap), checked before download; `resolve_bytes(drive_ref)` → bytes, `DriveFileTooLargeError` over `MAX_DRIVE_DOWNLOAD_BYTES` before download.

- [ ] **Step 1: Failing tests**

```python
"""drive: refs in FileConversionService (docs/10_rfcs/USER_DRIVE_RFC.md §4.6)."""
from unittest.mock import AsyncMock, patch

import pytest

from src.domain.user_drive import MAX_DRIVE_DOWNLOAD_BYTES, DriveFileTooLargeError, DriveItem
from src.ports.file_storage_port import FileStoragePort
from src.ports.user_drive_port import UserDrivePort
from src.services.file_conversion_service import FileConversionService
from src.utils.file_conversion import MAX_FILE_BYTES


def _item(size, name="notes.md", mime="text/markdown", folder=False):
    return DriveItem(item_id="i1", name=name, path=f"Docs/{name}", is_folder=folder,
                     size_bytes=size, mime_type=mime)


@pytest.fixture
def drive():
    return AsyncMock(spec=UserDrivePort)


@pytest.fixture
def svc(drive):
    return FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=drive)


class TestDriveRefs:
    async def test_text_uses_metadata_mime_and_path(self, svc, drive):
        drive.get_item.return_value = _item(20)
        drive.download.return_value = b"# hello"
        conv = AsyncMock(return_value="[File: Docs/notes.md]\n# hello\n[/File: Docs/notes.md]")
        with patch("src.services.file_conversion_service.convert_file_to_text", new=conv):
            out = await svc.resolve_content("drive:i1", "u1")
        assert "# hello" in out
        assert conv.call_args.args[1:3] == ("Docs/notes.md", "text/markdown")

    async def test_text_over_cap_refused_before_download(self, svc, drive):
        drive.get_item.return_value = _item(MAX_FILE_BYTES + 1)
        assert (await svc.resolve_content("drive:i1", "u1")).startswith("[System:")
        drive.download.assert_not_called()

    async def test_folder_is_not_readable(self, svc, drive):
        drive.get_item.return_value = _item(0, name="Docs", mime="", folder=True)
        out = await svc.resolve_content("drive:i1", "u1")
        assert "folder" in out and "list_files_in_drive" in out
        drive.download.assert_not_called()

    async def test_bytes_over_cap_raise_before_download(self, svc, drive):
        drive.get_item.return_value = _item(MAX_DRIVE_DOWNLOAD_BYTES + 1, name="v.mp4", mime="video/mp4")
        with pytest.raises(DriveFileTooLargeError):
            await svc.resolve_bytes("drive:i1", "u1")
        drive.download.assert_not_called()

    async def test_no_drive_wired_is_not_found(self):
        with pytest.raises(FileNotFoundError):
            await FileConversionService(storage=AsyncMock(spec=FileStoragePort)).get_drive_item("drive:i1", "u1")

    async def test_non_drive_refs_unchanged(self, svc, drive):
        svc._storage.download.return_value = b"x"
        await svc.resolve_bytes("report.docx", "u1")
        drive.get_item.assert_not_called()
```

- [ ] **Step 2:** `pytest tests/unit/services/test_file_conversion_service_drive_refs.py -v` → FAIL (unexpected kwarg `drive`).

- [ ] **Step 3: Implement** — imports next to the existing ones:

```python
from ..domain.user_drive import (
    MAX_DRIVE_DOWNLOAD_BYTES,
    DriveFileTooLargeError,
    DriveItem,
    format_size,
    parse_drive_ref,
)
from ..utils.file_conversion import MAX_FILE_BYTES
```

Under `TYPE_CHECKING`: `from ..ports.user_drive_port import UserDrivePort`. Constructor: add `drive: Optional["UserDrivePort"] = None` after `skill_files`, and `self._drive = drive  # drive:<id> refs — the user's long-term file area (USER_DRIVE_RFC §4.6)`. New methods:

```python
    async def get_drive_item(self, ref: str, user_id: str) -> DriveItem:
        item_id = parse_drive_ref(ref)
        if item_id is None or self._drive is None:
            raise FileNotFoundError(f"{ref!r} is not available on the user's drive")
        return await self._drive.get_item(user_id, item_id)

    async def _resolve_drive_content(self, ref: str, user_id: str) -> str:
        item = await self.get_drive_item(ref, user_id)
        if item.is_folder:
            return (f"[System: '{item.path or '/'}' is a folder, not a file. "
                    f"Use list_files_in_drive to see what is inside.]")
        if item.size_bytes > MAX_FILE_BYTES:
            return (f"[System: '{item.path}' is {format_size(item.size_bytes)}; files above "
                    f"{format_size(MAX_FILE_BYTES)} cannot be read as text yet. Long audio and video "
                    f"processing is not available yet.]")
        assert self._drive is not None
        data = await self._drive.download(user_id, item.item_id)
        mime_type = item.mime_type or (mimetypes.guess_type(item.name)[0] or "application/octet-stream")
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(item.name)[1] or ".bin")
        try:
            os.close(tmp_fd)
            async with aiofiles.open(tmp_path, "wb") as f:
                await f.write(data)
            return await convert_file_to_text(tmp_path, item.path, mime_type, audio_service=self._audio_service)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                logger.debug("Failed to remove temp file %s", tmp_path)

    async def _resolve_drive_bytes(self, ref: str, user_id: str) -> bytes:
        item = await self.get_drive_item(ref, user_id)
        if item.size_bytes > MAX_DRIVE_DOWNLOAD_BYTES:
            raise DriveFileTooLargeError(item, MAX_DRIVE_DOWNLOAD_BYTES)
        assert self._drive is not None
        return await self._drive.download(user_id, item.item_id)
```

At the top of `resolve_content` (after the skill branch) and of `resolve_bytes`:

```python
        if parse_drive_ref(ref) is not None:
            return await self._resolve_drive_content(ref, user_id)
```

```python
        if parse_drive_ref(ref) is not None:
            return await self._resolve_drive_bytes(ref, user_id)
```

(`mimetypes`, `os`, `tempfile`, `aiofiles`, `convert_file_to_text` are already imported in this module — check with `grep -n "^import\|^from\|^    convert_file_to_text" src/services/file_conversion_service.py`.)

- [ ] **Step 4:** `pytest tests/unit/services/test_file_conversion_service_drive_refs.py tests/unit/services/test_file_conversion_service.py tests/unit/services/test_file_conversion_service_gcs.py tests/unit/services/test_file_conversion_service_skill_refs.py -v` → PASS (existing unchanged; reviewer rule otherwise).

- [ ] **Step 5: Commit** — `git add src/services/file_conversion_service.py tests/unit/services/test_file_conversion_service_drive_refs.py && git commit -m "feat(drive): read drive: refs with every size check before download"`

---

## Task 7: Registry flag, coordinator, manifest intents

**Files:**
- Modify: `src/infrastructure/agent_registry.py` (`AgentDescriptor`, after `eager`)
- Modify: `src/infrastructure/agent_coordinator.py` (`_execute_sync` ~line 523, `_execute_async` ~line 545)
- Modify: `src/infrastructure/agent_manifest.py` (`class Intent` ~line 76; `FILE_MANAGEMENT` ~line 506)
- Test: `tests/unit/infrastructure/test_coordinator_prefetch_flag.py`, `tests/unit/infrastructure/test_agent_manifest_drive.py`

**Interfaces — produces:** `AgentDescriptor.prefetch_file_ref: bool = True`; `AgentCoordinator._maybe_resolve_file_refs(base_agent_id, params, user_id)`; `Intent.*_DRIVE` constants; context fields used by Task 8: `file_ref`, `folder`, `name`, `new_name`, `search_text`, `append_text`, `source_ref`.

- [ ] **Step 1: Failing tests**

```python
"""prefetch_file_ref (docs/10_rfcs/USER_DRIVE_RFC.md §4.4)."""
from unittest.mock import AsyncMock

from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentDescriptor, AgentRegistry, ExecutionMode


def _coordinator(prefetch: bool):
    registry = AgentRegistry()
    registry.register(AgentDescriptor(agent_id="files", agent_type="files",
                                      capabilities={"open_x": ExecutionMode.SYNC}, prefetch_file_ref=prefetch))
    coord = AgentCoordinator(registry=registry)
    coord._file_ref_resolver = AsyncMock(return_value="text")
    return coord


async def test_default_still_prefetches():
    coord = _coordinator(True)
    params = {"file_ref": "a.txt"}
    await coord._maybe_resolve_file_refs("files", params, "u1")
    assert params["file_content"] == "text"


async def test_flag_off_skips_prefetch():
    coord = _coordinator(False)
    params = {"file_ref": "drive:abc"}
    await coord._maybe_resolve_file_refs("files", params, "u1")
    coord._file_ref_resolver.assert_not_called()
    assert "file_content" not in params


async def test_unknown_agent_keeps_old_behaviour():
    coord = _coordinator(False)
    params = {"file_ref": "a.txt"}
    await coord._maybe_resolve_file_refs("other", params, "u1")
    assert params["file_content"] == "text"
```

```python
"""Drive intents on FILE_MANAGEMENT (docs/10_rfcs/USER_DRIVE_RFC.md §3, §4.3–§4.5, §4.9)."""
import re

from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent
from src.infrastructure.agent_registry import ExecutionMode

_DRIVE = {
    Intent.SAVE_FILE_TO_DRIVE: "save_file_to_drive",
    Intent.LIST_FILES_IN_DRIVE: "list_files_in_drive",
    Intent.SEARCH_FILES_IN_DRIVE: "search_files_in_drive",
    Intent.OPEN_FILE_FROM_DRIVE: "open_file_from_drive",
    Intent.MOVE_FILE_IN_DRIVE: "move_file_in_drive",
    Intent.CREATE_FOLDER_IN_DRIVE: "create_folder_in_drive",
    Intent.UPDATE_FILE_IN_DRIVE: "update_file_in_drive",
    Intent.DELETE_FILE_FROM_DRIVE: "delete_file_from_drive",
}
_PROVIDER = re.compile(r"onedrive|microsoft|google", re.I)


def test_values_and_sync():
    for const, value in _DRIVE.items():
        assert const == value
        assert FILE_MANAGEMENT.capabilities[const] == ExecutionMode.SYNC


def test_no_prefetch():
    assert FILE_MANAGEMENT.prefetch_file_ref is False


def test_schemas():
    s = FILE_MANAGEMENT.context_schemas
    assert {"file_ref", "folder", "name"} <= set(s[Intent.SAVE_FILE_TO_DRIVE])
    assert {"file_ref", "folder", "new_name"} <= set(s[Intent.MOVE_FILE_IN_DRIVE])
    assert "search_text" in s[Intent.SEARCH_FILES_IN_DRIVE]
    assert set(s[Intent.UPDATE_FILE_IN_DRIVE]) == {"file_ref", "append_text", "source_ref"}


def test_no_provider_name_reaches_the_model():
    texts = [FILE_MANAGEMENT.description]
    texts += [FILE_MANAGEMENT.capability_descriptions[i] for i in FILE_MANAGEMENT.capabilities]
    for schema in FILE_MANAGEMENT.context_schemas.values():
        texts += [str(v) for v in schema.values()]
    for text in texts:
        assert not _PROVIDER.search(text), text


def test_save_says_chat_files_are_temporary():
    text = FILE_MANAGEMENT.capability_descriptions[Intent.SAVE_FILE_TO_DRIVE].lower()
    assert "temporary" in text and "remember" in text
```

- [ ] **Step 2:** `pytest tests/unit/infrastructure/test_coordinator_prefetch_flag.py tests/unit/infrastructure/test_agent_manifest_drive.py -v` → FAIL.

- [ ] **Step 3: Registry flag + coordinator**

`agent_registry.py`, in `AgentDescriptor` after `eager`:

```python
    # Pre-fetch: AgentCoordinator downloads and converts any `file_ref` before dispatch and puts the
    # text into `file_content`. An agent that reads files itself (FileManagement) opts out — otherwise
    # a delete would first download the file it deletes (USER_DRIVE_RFC §4.4).
    prefetch_file_ref: bool = True
```

`agent_coordinator.py`, next to `_resolve_file_refs`:

```python
    async def _maybe_resolve_file_refs(self, base_agent_id: str, params: Dict[str, Any], user_id: str) -> None:
        """Pre-fetch file_ref unless the target agent opted out (AgentDescriptor.prefetch_file_ref)."""
        descriptor = self._registry.get_descriptor(base_agent_id) if self._registry else None
        if descriptor is not None and not descriptor.prefetch_file_ref:
            return
        await self._resolve_file_refs(params, user_id)
```

Call sites: `_execute_sync`: `await self._resolve_file_refs(extra_payload, user_id)` → `await self._maybe_resolve_file_refs(base_agent_id, extra_payload, user_id)`; `_execute_async`: → `await self._maybe_resolve_file_refs(base_agent_id, extra_payload, context.get("user_id", ""))`. Confirm the attribute is `self._registry` (`grep -n "self._registry" src/infrastructure/agent_coordinator.py`).

- [ ] **Step 4: Manifest**

`class Intent`, after `DELETE_FILE`:

```python
    # The user's drive — long-term file area (USER_DRIVE_RFC §3, §4.4). The store is in the name.
    SAVE_FILE_TO_DRIVE     = "save_file_to_drive"
    LIST_FILES_IN_DRIVE    = "list_files_in_drive"
    SEARCH_FILES_IN_DRIVE  = "search_files_in_drive"
    OPEN_FILE_FROM_DRIVE   = "open_file_from_drive"
    MOVE_FILE_IN_DRIVE     = "move_file_in_drive"
    CREATE_FOLDER_IN_DRIVE = "create_folder_in_drive"
    UPDATE_FILE_IN_DRIVE   = "update_file_in_drive"
    DELETE_FILE_FROM_DRIVE = "delete_file_from_drive"
```

`FILE_MANAGEMENT`: add the eight intents to `capabilities` as `ExecutionMode.SYNC`; `prefetch_file_ref=False`; `description="File storage archivist: chat files (temporary) and the user's drive (long-term file area)"`; `capability_descriptions` additions:

```python
        Intent.SAVE_FILE_TO_DRIVE: (
            "Keep a file on the user's drive, their long-term file area. Chat attachments ([File: ...]) and "
            "documents you produced are temporary; to remember, save, keep or put a FILE anywhere, always use "
            "this intent (facts and text go to save_to_memory). Saves to Inbox unless the user named a folder. "
            "Never overwrites: the reply states the name actually used. "
            'Requires: context={"file_ref": "<ref from [File: ...] or a delivered document>"}; optional '
            '"folder" ONLY when the user named one; optional "name" for a clear, human-readable file name.'
        ),
        Intent.LIST_FILES_IN_DRIVE: (
            "List a folder on the user's drive (folders first). Results are [Drive: ...] labels with refs. "
            'Optional context={"folder": "<path>"}; omit for the top level.'
        ),
        Intent.SEARCH_FILES_IN_DRIVE: (
            "Find files anywhere on the user's drive, all subfolders. <SPIKE A6 SENTENCE> A file saved in the last "
            "minute may not be found yet. Chat attachments are not searchable. "
            'Requires: context={"search_text": "<words>"}'
        ),
        Intent.OPEN_FILE_FROM_DRIVE: (
            "Open a file from the user's drive and return its content (text, or the image/PDF itself). "
            'Requires: context={"file_ref": "drive:<id> from a [Drive: ...] label"}'
        ),
        Intent.MOVE_FILE_IN_DRIVE: (
            "Move and/or rename a file or folder already on the user's drive. A chat file is not on the drive: "
            "use save_file_to_drive for it. "
            'Requires: context={"file_ref": "drive:<id>"} plus "folder" and/or "new_name".'
        ),
        Intent.CREATE_FOLDER_IN_DRIVE: (
            "Create a folder (and missing parents) on the user's drive. "
            'Requires: context={"folder": "<path>"}'
        ),
        Intent.UPDATE_FILE_IN_DRIVE: (
            "Change a file on the user's drive in one of two safe ways: append text at the end of a text file "
            '("append_text"), or replace the whole content with another file ("source_ref", e.g. a new version '
            "the user sent or a document you just produced; the user gets a notice with old and new size). "
            "Rewriting a file with new text is not available. "
            'Requires: context={"file_ref": "drive:<id>"} plus exactly one of "append_text" or "source_ref".'
        ),
        Intent.DELETE_FILE_FROM_DRIVE: (
            "Delete a file or a whole folder from the user's drive (recoverable from its recycle bin); the user "
            "gets a notice with what was deleted. "
            'Requires: context={"file_ref": "drive:<id>"}'
        ),
```

Replace `<SPIKE A6 SENTENCE>` with "Matches file names." or "Matches file names and content." per Task 0. If Task 0 removed search, drop `SEARCH_FILES_IN_DRIVE` from capabilities, descriptions, schemas and the test dict.

`context_schemas` additions:

```python
        Intent.SAVE_FILE_TO_DRIVE: {
            "file_ref": "Ref of the file to keep: name from a [File: ...] label, or a delivered document key",
            "folder": "Optional. Folder path on the drive, only if the user named one (e.g. 'Meetings/2026')",
            "name": "Optional. Clear, human-readable file name; the extension is kept from the source if omitted",
        },
        Intent.LIST_FILES_IN_DRIVE: {"folder": "Optional. Folder path on the drive; omit for the top level"},
        Intent.SEARCH_FILES_IN_DRIVE: {"search_text": "Words to search for"},
        Intent.OPEN_FILE_FROM_DRIVE: {"file_ref": "drive:<id> ref from a [Drive: ...] label"},
        Intent.MOVE_FILE_IN_DRIVE: {
            "file_ref": "drive:<id> ref of the file or folder",
            "folder": "Optional. Destination folder path",
            "new_name": "Optional. New name, extension included for files",
        },
        Intent.CREATE_FOLDER_IN_DRIVE: {"folder": "Folder path to create, e.g. 'Contracts/2026'"},
        Intent.UPDATE_FILE_IN_DRIVE: {
            "file_ref": "drive:<id> ref of the file to change",
            "append_text": "Optional. Text to add at the end (text files only)",
            "source_ref": "Optional. Ref of a file whose content replaces the whole file",
        },
        Intent.DELETE_FILE_FROM_DRIVE: {"file_ref": "drive:<id> ref of the file or folder"},
```

- [ ] **Step 5:** `pytest tests/unit/infrastructure/ -v` → PASS (existing `test_agent_manifest.py`, `test_agent_registry.py`, `test_agent_coordinator.py` unchanged).

- [ ] **Step 6: Commit** — `git add src/infrastructure/ tests/unit/infrastructure/test_coordinator_prefetch_flag.py tests/unit/infrastructure/test_agent_manifest_drive.py && git commit -m "feat(drive): eight drive intents; FILE_MANAGEMENT opts out of file_ref pre-fetch"`

---

## Task 8: `FileManagementAgent` — drive intents, store guards, shielded mutations, receipts

**Files:**
- Modify: `src/agents/file_management_agent.py`
- Modify: `src/domain/ui_messages.py`, `src/locales/{en,uk,fr,es}.py`
- Test: `tests/unit/agents/test_file_management_agent_drive.py`
- Must stay green unchanged: `tests/unit/agents/test_file_management_agent.py`, `tests/unit/agents/test_file_management_agent_skill_refs.py`, `tests/unit/test_req_ui_06_localization.py`

**Interfaces:**
- Consumes: Tasks 1, 5, 6, 7; `UserNotificationService.notify_raw(user_id, account_id, text, channel_id_override=None, platform_override=None)`; `LocalizationService.get_ui_string(lang, UIMessage) -> str`; `LanguageServicePort.resolve_ui_language(user_id)`.
- Produces: constructor kwargs `drive_service`, `localization`, `language_service` (all optional); `UIMessage.DRIVE_DELETED_FILE` (`{path}`), `DRIVE_DELETED_FOLDER` (`{path}`, `{count}`), `DRIVE_REPLACED` (`{path}`, `{before}`, `{after}`).

Behaviour table (exact):

| Intent | Ref rule | Success result | Receipt | Shielded |
|---|---|---|---|---|
| `open_file` + `drive:` ref | lenient → drive open | as below | — | — |
| `open_file_from_drive` | lenient: non-drive → existing `_fetch` | text / image-PDF file_data / video resend; caps per §4.6 | — | — |
| `delete_file` + `drive:` ref | refuse → hint `delete_file_from_drive` | — | — | — |
| `save_file_to_drive` | `file_ref` must NOT be drive (hint `move_file_in_drive`) | `Saved <label>.` / `Already on the drive: <label>.` / `Saved as "<name>" (the name "<wanted>" was taken): <label>.` + created folders | — | yes |
| `list_files_in_drive` | — | folder label + children + truncation note | — | — |
| `search_files_in_drive` | `search_text` required | labels, or `No files on the drive matched '<q>'.` | — | — |
| `move_file_in_drive` | drive ref | `Moved: <old path> → <label>.` | — | yes |
| `create_folder_in_drive` | `folder` required | folder label + created | — | yes |
| `update_file_in_drive` | drive ref; exactly one of `append_text` / `source_ref` | `Appended to <label>.` / `Replaced <label>.` | replace only (`DRIVE_REPLACED`) | yes |
| `delete_file_from_drive` | drive ref | `Deleted <path>.` / `Deleted folder <path> (<n> files).` | `DRIVE_DELETED_FILE` / `_FOLDER` | yes |

Mutations (rows marked shielded) of one user run one at a time under a per-user `asyncio.Lock`; the agent waits for a mutation at most `MUTATION_WAIT_S = 100.0` s and otherwise returns a **success** with `STILL_RUNNING` ("The drive operation is still running and may complete. Check with list_files_in_drive or search_files_in_drive before retrying; a notice follows for deletes and replaces."). A success, not a failure: a failure is turned into "rejected… try again" by `DelegationEngine`. A shielded task logs its own outcome when it ends (done-callback). Vision only for `VISION_IMAGE_MIME_TYPES` and PDF; other images (HEIC…) → success "'<path>' is in a format that cannot be viewed yet (<mime>).".

Errors → `AgentResponse.failure`: `DriveNotConnectedError` → "The user's drive is not connected or its access expired. Ask them to connect it in the Cabinet."; `DriveItemNotFoundError` → "That item is no longer on the drive (deleted or moved out). Search or list again."; `DriveRootProtectedError` / `DrivePathError` → `str(exc)`; no drive wired → "The drive is not configured."; anything else → logged, "Drive operation failed: <type>." `DriveFileTooLargeError` on open → success "'<path>' is <size>; files above <limit> cannot be opened yet."

- [ ] **Step 1: UI strings**

`src/domain/ui_messages.py`, end of `UIMessage`:

```python
    # User drive receipts (USER_DRIVE_RFC §4.10) — destructive actions only.
    DRIVE_DELETED_FILE = "drive_deleted_file"
    DRIVE_DELETED_FOLDER = "drive_deleted_folder"
    DRIVE_REPLACED = "drive_replaced"
```

`UI_STRINGS` entries:

```python
# en.py
    UIMessage.DRIVE_DELETED_FILE.value: "🗑️ Deleted from the drive: {path}",
    UIMessage.DRIVE_DELETED_FOLDER.value: "🗑️ Deleted folder: {path} (files: {count})",
    UIMessage.DRIVE_REPLACED.value: "✏️ Replaced: {path} ({before} → {after}; the previous version is in its version history)",
# uk.py
    UIMessage.DRIVE_DELETED_FILE.value: "🗑️ Видалено з диска: {path}",
    UIMessage.DRIVE_DELETED_FOLDER.value: "🗑️ Видалено папку: {path} (файлів: {count})",
    UIMessage.DRIVE_REPLACED.value: "✏️ Замінено: {path} ({before} → {after}; попередня версія — в історії версій)",
# fr.py
    UIMessage.DRIVE_DELETED_FILE.value: "🗑️ Supprimé du disque : {path}",
    UIMessage.DRIVE_DELETED_FOLDER.value: "🗑️ Dossier supprimé : {path} (fichiers : {count})",
    UIMessage.DRIVE_REPLACED.value: "✏️ Remplacé : {path} ({before} → {after} ; la version précédente est dans l'historique des versions)",
# es.py
    UIMessage.DRIVE_DELETED_FILE.value: "🗑️ Eliminado del disco: {path}",
    UIMessage.DRIVE_DELETED_FOLDER.value: "🗑️ Carpeta eliminada: {path} (archivos: {count})",
    UIMessage.DRIVE_REPLACED.value: "✏️ Reemplazado: {path} ({before} → {after}; la versión anterior está en su historial de versiones)",
```

- [ ] **Step 2: Failing agent tests**

```python
"""Drive intents in FileManagementAgent (docs/10_rfcs/USER_DRIVE_RFC.md §4.4–§4.13)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.domain.user_drive import (
    DeleteOutcome,
    DriveItem,
    DriveNotConnectedError,
    DriveRootProtectedError,
    ListOutcome,
    MoveOutcome,
    SaveOutcome,
    UpdateOutcome,
)
from src.infrastructure.agent_manifest import Intent
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

FILE = DriveItem(item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", is_folder=False,
                 size_bytes=2048, mime_type="application/pdf")
NOTE = DriveItem(item_id="n1", name="todo.md", path="Notes/todo.md", is_folder=False,
                 size_bytes=10, mime_type="text/markdown")
FOLDER = DriveItem(item_id="d1", name="2025", path="Meetings/2025", is_folder=True)
BIG_IMAGE = DriveItem(item_id="i1", name="scan.png", path="scan.png", is_folder=False,
                      size_bytes=6 * 1024 * 1024, mime_type="image/png")
HEIC = DriveItem(item_id="h1", name="IMG_0001.HEIC", path="IMG_0001.HEIC", is_folder=False,
                 size_bytes=1024, mime_type="image/heic")


def _msg(intent, **payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "t1"
    msg.intent = AgentIntent.QUERY
    msg.payload = {"intent": intent, **payload}
    msg.context = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
    return msg


@pytest.fixture
def conversion():
    return AsyncMock(spec=FileConversionService)


@pytest.fixture
def drive():
    return AsyncMock(spec=UserDriveService)


@pytest.fixture
def notifier():
    return AsyncMock()


@pytest.fixture
def agent(conversion, drive, notifier):
    localization = MagicMock()
    localization.get_ui_string.side_effect = lambda lang, m: {
        UIMessage.DRIVE_DELETED_FILE: "DEL {path}",
        UIMessage.DRIVE_DELETED_FOLDER: "DELDIR {path} {count}",
        UIMessage.DRIVE_REPLACED: "REP {path} {before} {after}",
    }[m]
    language = AsyncMock()
    language.resolve_ui_language.return_value = LanguageCode.UK
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management",
                           capabilities={}, metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort),
        notification=notifier, drive_service=drive, localization=localization, language_service=language,
    )


class TestStoreGuards:
    async def test_delete_file_refuses_drive_ref(self, agent, drive):
        resp = await agent.execute(_msg(Intent.DELETE_FILE, file_ref="drive:f1"))
        assert resp.status == AgentStatus.FAILED and "delete_file_from_drive" in resp.error
        drive.delete.assert_not_called()

    async def test_mutating_drive_intents_refuse_chat_ref(self, agent):
        for intent in (Intent.MOVE_FILE_IN_DRIVE, Intent.DELETE_FILE_FROM_DRIVE, Intent.UPDATE_FILE_IN_DRIVE):
            resp = await agent.execute(_msg(intent, file_ref="report.docx", folder="X", append_text="t"))
            assert resp.status == AgentStatus.FAILED and "save_file_to_drive" in resp.error

    async def test_save_refuses_drive_ref(self, agent):
        resp = await agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="drive:f1"))
        assert resp.status == AgentStatus.FAILED and "move_file_in_drive" in resp.error

    async def test_open_file_is_lenient_with_drive_ref(self, agent, conversion):
        conversion.get_drive_item.return_value = NOTE
        conversion.resolve_content.return_value = "[File: Notes/todo.md]\nx\n[/File: Notes/todo.md]"
        resp = await agent.execute(_msg(Intent.OPEN_FILE, file_ref="drive:n1"))
        assert resp.status == AgentStatus.SUCCESS
        conversion.resolve_content.assert_awaited_once_with("drive:n1", "u1")


class TestOpenCaps:
    async def test_large_image_refused_without_download(self, agent, conversion):
        conversion.get_drive_item.return_value = BIG_IMAGE
        resp = await agent.execute(_msg(Intent.OPEN_FILE_FROM_DRIVE, file_ref="drive:i1"))
        assert resp.status == AgentStatus.SUCCESS and "cannot be opened yet" in resp.result
        conversion.resolve_bytes.assert_not_called()

    async def test_heic_refused_without_download(self, agent, conversion):
        conversion.get_drive_item.return_value = HEIC
        resp = await agent.execute(_msg(Intent.OPEN_FILE_FROM_DRIVE, file_ref="drive:h1"))
        assert resp.status == AgentStatus.SUCCESS and "cannot be viewed yet" in resp.result
        conversion.resolve_bytes.assert_not_called()
        conversion.resolve_content.assert_not_called()


class TestSaveListSearch:
    async def test_save_uses_readable_name_and_history_context(self, agent, conversion, drive):
        conversion.resolve_bytes.return_value = b"%PDF"
        drive.save.return_value = SaveOutcome(item=FILE, created=["Inbox"])
        ref = "docs/u1/3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b-lease.pdf"
        resp = await agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref=ref))
        drive.save.assert_awaited_once_with("u1", b"%PDF", "lease.pdf", "application/pdf", None)
        assert "[Drive: Inbox/lease.pdf" in resp.result
        assert resp.history_context == {"drive_context": [{"path": "Inbox/lease.pdf", "ref": "drive:f1"}]}

    async def test_save_already_existed_and_renamed_messages(self, agent, conversion, drive):
        conversion.resolve_bytes.return_value = b"x"
        drive.save.return_value = SaveOutcome(item=FILE, created=[], already_existed=True)
        resp = await agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="lease.pdf"))
        assert resp.result.startswith("Already on the drive")
        renamed = DriveItem(item_id="f2", name="lease 1.pdf", path="Inbox/lease 1.pdf", is_folder=False)
        drive.save.return_value = SaveOutcome(item=renamed, created=[], renamed=True)
        resp = await agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="lease.pdf"))
        assert 'Saved as "lease 1.pdf"' in resp.result

    async def test_search_empty_is_success(self, agent, drive):
        drive.search.return_value = []
        resp = await agent.execute(_msg(Intent.SEARCH_FILES_IN_DRIVE, search_text="lease"))
        assert resp.status == AgentStatus.SUCCESS and "No files on the drive matched 'lease'" in resp.result

    async def test_list_labels(self, agent, drive):
        root = DriveItem(item_id="r", name="Alek-bot", path="", is_folder=True)
        drive.list_folder.return_value = ListOutcome(folder=root, items=[FOLDER, FILE], truncated=False)
        resp = await agent.execute(_msg(Intent.LIST_FILES_IN_DRIVE))
        assert "[Drive folder: Meetings/2025/" in resp.result and "[Drive: Inbox/lease.pdf" in resp.result


class TestReceipts:
    async def test_delete_folder_receipt_with_count(self, agent, drive, notifier):
        drive.delete.return_value = DeleteOutcome(item=FOLDER, file_count=14)
        resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:d1"))
        assert resp.status == AgentStatus.SUCCESS
        notifier.notify_raw.assert_awaited_once_with(
            "u1", "a1", "DELDIR Meetings/2025 14", channel_id_override="C1", platform_override="slack")

    async def test_replace_receipt_with_sizes(self, agent, conversion, drive, notifier):
        conversion.resolve_bytes.return_value = b"x" * 3000
        after = DriveItem(item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", is_folder=False,
                          size_bytes=3000, mime_type="application/pdf")
        drive.get_item.return_value = FILE
        drive.replace_with.return_value = UpdateOutcome(item=after, before_size=2048, mode="replace")
        resp = await agent.execute(_msg(Intent.UPDATE_FILE_IN_DRIVE, file_ref="drive:f1", source_ref="new.pdf"))
        assert resp.status == AgentStatus.SUCCESS
        assert notifier.notify_raw.call_args.args[2] == "REP Inbox/lease.pdf 2KB 3KB"

    async def test_append_has_no_receipt(self, agent, drive, notifier):
        drive.append_text.return_value = UpdateOutcome(item=NOTE, before_size=10, mode="append")
        resp = await agent.execute(_msg(Intent.UPDATE_FILE_IN_DRIVE, file_ref="drive:n1", append_text="- two"))
        assert resp.status == AgentStatus.SUCCESS and resp.result.startswith("Appended to")
        drive.append_text.assert_awaited_once_with("u1", "n1", "- two")
        notifier.notify_raw.assert_not_called()

    async def test_update_needs_exactly_one_mode(self, agent):
        resp = await agent.execute(_msg(Intent.UPDATE_FILE_IN_DRIVE, file_ref="drive:n1"))
        assert resp.status == AgentStatus.FAILED and "exactly one" in resp.error

    async def test_move_has_no_receipt(self, agent, drive, notifier):
        moved = DriveItem(item_id="f1", name="lease.pdf", path="Contracts/lease.pdf", is_folder=False)
        drive.move.return_value = MoveOutcome(before=FILE, after=moved, created=[])
        resp = await agent.execute(_msg(Intent.MOVE_FILE_IN_DRIVE, file_ref="drive:f1", folder="Contracts"))
        assert "Inbox/lease.pdf → [Drive: Contracts/lease.pdf" in resp.result
        notifier.notify_raw.assert_not_called()


class TestShield:
    async def test_cancelled_delete_still_completes_with_receipt(self, agent, drive, notifier):
        gate = asyncio.Event()

        async def slow_delete(user_id, item_id):
            await gate.wait()
            return DeleteOutcome(item=FILE, file_count=1)

        drive.delete.side_effect = slow_delete
        task = asyncio.create_task(agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1")))
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        notifier.notify_raw.assert_awaited_once()


class TestMutationBudget:
    async def test_slow_mutation_answers_still_running(self, agent, drive, notifier):
        gate = asyncio.Event()

        async def slow_delete(user_id, item_id):
            await gate.wait()
            return DeleteOutcome(item=FILE, file_count=1)

        drive.delete.side_effect = slow_delete
        agent.MUTATION_WAIT_S = 0.01
        resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
        assert resp.status == AgentStatus.SUCCESS and "still running" in resp.result
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        notifier.notify_raw.assert_awaited_once()  # the receipt still arrives

    async def test_one_users_mutations_run_one_at_a_time(self, agent, conversion, drive):
        conversion.resolve_bytes.return_value = b"x"
        active, peak = 0, 0

        async def save(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return SaveOutcome(item=FILE, created=[])

        drive.save.side_effect = save
        await asyncio.gather(*(agent.execute(_msg(Intent.SAVE_FILE_TO_DRIVE, file_ref="a.pdf")) for _ in range(3)))
        assert peak == 1


class TestErrors:
    async def test_not_connected_message(self, agent, drive):
        drive.search.side_effect = DriveNotConnectedError("x")
        resp = await agent.execute(_msg(Intent.SEARCH_FILES_IN_DRIVE, search_text="a"))
        assert resp.status == AgentStatus.FAILED and "connect it in the Cabinet" in resp.error

    async def test_root_protected_passthrough(self, agent, drive):
        drive.delete.side_effect = DriveRootProtectedError("The drive area itself cannot be deleted or moved")
        resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:r"))
        assert resp.status == AgentStatus.FAILED and "cannot be deleted" in resp.error
```

- [ ] **Step 3:** `pytest tests/unit/agents/test_file_management_agent_drive.py -v` → FAIL.

- [ ] **Step 4: Implement**

Module docstring intent list:

```
Intents:
  open_file / delete_file         — chat files, delivered documents, skill files (GCS);
                                    open_file also opens drive: refs (lenient read)
  save_file_to_drive              — chat file / delivered document → the user's drive
  list_files_in_drive, search_files_in_drive, open_file_from_drive
  move_file_in_drive, create_folder_in_drive
  update_file_in_drive (append / replace-by-file), delete_file_from_drive
Mutating intents refuse a ref from the other store and run shielded from cancellation;
delete and replace post a receipt via notify_raw (USER_DRIVE_RFC §4.4, §4.10, §4.13).
```

Imports (add): `import asyncio`; `from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Set`; `from ..domain.language import LanguageCode`; `from ..domain.ui_messages import UIMessage`;

```python
from ..domain.user_drive import (
    MAX_DRIVE_VISION_IMAGE_BYTES,
    MAX_DRIVE_VISION_PDF_BYTES,
    VISION_IMAGE_MIME_TYPES,
    DriveFileTooLargeError,
    DriveItem,
    DriveItemNotFoundError,
    DriveNotConnectedError,
    DrivePathError,
    DriveRootProtectedError,
    drive_context_entry,
    drive_filename,
    drive_label,
    format_size,
    is_drive_ref,
    parse_drive_ref,
)
```

and under `TYPE_CHECKING`: `from ..ports.language_service_port import LanguageServicePort`, `from ..services.localization_service import LocalizationService`, `from ..services.user_drive_service import UserDriveService`.

Constructor additions and state:

```python
        drive_service: Optional["UserDriveService"] = None,
        localization: Optional["LocalizationService"] = None,
        language_service: Optional["LanguageServicePort"] = None,
    ) -> None:
        ...
        self._drive = drive_service
        self._localization = localization
        self._language = language_service
        # Shielded mutations still running after their caller stopped waiting (§4.13). Kept so the
        # tasks are not garbage-collected mid-flight (RUF006); only touched on the event loop.
        self._shielded: Set["asyncio.Task[Any]"] = set()
        # One user's drive mutations run one at a time (§4.13): a mid-flight retry waits for the
        # first, then the duplicate check sees it.
        self._mutation_locks: Dict[str, asyncio.Lock] = {}
```

Class constants:

```python
    _NOT_CONNECTED = ("The user's drive is not connected or its access expired. "
                      "Ask them to connect it in the Cabinet.")
    _GONE = "That item is no longer on the drive (deleted or moved out). Search or list again."
    # How long the agent waits for a mutation — inside its own 120 s timeout, so BaseAgent's
    # timeout (a false "failed" + a circuit-breaker failure) never fires for one (§4.13).
    MUTATION_WAIT_S = 100.0
    STILL_RUNNING = ("The drive operation is still running and may complete. Check with list_files_in_drive or "
                     "search_files_in_drive before retrying; a notice follows for deletes and replaces.")
```

`execute` dispatch (replace the two existing `if intent == …` blocks, keep the unknown-intent failure after, listing all supported intents):

```python
        if intent == Intent.OPEN_FILE:
            if is_drive_ref(payload.get("file_ref") or ""):
                return await self._drive_call(message, self._open_drive, payload, user_id)
            return await self._fetch(message, payload, user_id)

        if intent == Intent.DELETE_FILE:
            ref = payload.get("file_ref") or ""
            if is_drive_ref(ref):
                return self._fail(message, f"'{ref}' is on the user's drive; use delete_file_from_drive.")
            return await self._delete(message, payload, user_id)

        reads = {
            Intent.OPEN_FILE_FROM_DRIVE: self._open_drive,
            Intent.LIST_FILES_IN_DRIVE: self._list_drive,
            Intent.SEARCH_FILES_IN_DRIVE: self._search_drive,
        }
        writes = {
            Intent.SAVE_FILE_TO_DRIVE: self._save_to_drive,
            Intent.MOVE_FILE_IN_DRIVE: self._move_in_drive,
            Intent.CREATE_FOLDER_IN_DRIVE: self._create_folder_in_drive,
            Intent.UPDATE_FILE_IN_DRIVE: self._update_in_drive,
            Intent.DELETE_FILE_FROM_DRIVE: self._delete_from_drive,
        }
        if intent in reads:
            return await self._drive_call(message, reads[intent], payload, user_id)
        if intent in writes:
            return await self._drive_call(message, writes[intent], payload, user_id, shielded=True)
```

Helpers and handlers:

```python
    def _fail(self, message: AgentMessage, error: str) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id, error=error)

    def _ok(self, message: AgentMessage, result: str, touched: list[DriveItem], **extra: Any) -> AgentResponse:
        self._on_agent_success(char_count=len(result), output_text=result[:200])
        return AgentResponse.success(
            task_id=message.task_id, agent_id=self.agent_id, result=result, confidence=1.0,
            history_context={"drive_context": [drive_context_entry(i) for i in touched]} if touched else None,
            **extra,
        )

    def _wrong_store(self, message: AgentMessage, ref: str) -> AgentResponse:
        return self._fail(message, f"'{ref}' is a chat file, not on the user's drive. "
                                   f"To put it there use save_file_to_drive.")

    async def _drive_call(self, message: AgentMessage,
                          handler: Callable[[AgentMessage, dict, str], Awaitable[AgentResponse]],
                          payload: dict, user_id: str, *, shielded: bool = False) -> AgentResponse:
        if self._drive is None:
            return self._fail(message, "The drive is not configured.")
        self._on_agent_start(f"{payload.get('intent')}: {payload.get('file_ref') or payload.get('folder') or ''}")
        try:
            if not shielded:
                return await handler(message, payload, user_id)
            lock = self._mutation_locks.setdefault(user_id, asyncio.Lock())

            async def serialized() -> AgentResponse:
                async with lock:
                    return await handler(message, payload, user_id)

            # The mutation and its receipt finish whatever happens to this call (§4.13).
            task = asyncio.ensure_future(serialized())
            self._shielded.add(task)
            task.add_done_callback(self._on_mutation_done)
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=self.MUTATION_WAIT_S)
            except asyncio.TimeoutError:
                logger.warning("FileManagementAgent: %s still running after %.0fs", payload.get("intent"),
                               self.MUTATION_WAIT_S)
                return self._ok(message, self.STILL_RUNNING, [])
        except DriveNotConnectedError:
            return self._fail(message, self._NOT_CONNECTED)
        except DriveItemNotFoundError:
            return self._fail(message, self._GONE)
        except (DriveRootProtectedError, DrivePathError) as e:
            return self._fail(message, str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("FileManagementAgent: drive op failed: %s", e, exc_info=True)
            self._on_agent_error(e, str(payload.get("intent")))
            return self._fail(message, f"Drive operation failed: {type(e).__name__}.")

    def _on_mutation_done(self, task: "asyncio.Task[Any]") -> None:
        """Log how a shielded mutation ended — nobody may be awaiting it any more (§4.13)."""
        self._shielded.discard(task)
        if task.cancelled():
            logger.warning("FileManagementAgent: drive mutation task was cancelled")
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("FileManagementAgent: drive mutation ended with %s: %s", type(exc).__name__, exc)
        else:
            logger.info("FileManagementAgent: drive mutation completed")

    async def _open_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        ref = payload.get("file_ref") or ""
        if not is_drive_ref(ref):
            return await self._fetch(message, payload, user_id)  # lenient read
        item = await self._conversion_service.get_drive_item(ref, user_id)
        if item.is_folder:
            return self._fail(message, f"'{item.path or '/'}' is a folder; use list_files_in_drive.")
        mime = item.mime_type or (mimetypes.guess_type(item.name)[0] or "application/octet-stream")
        if mime.startswith("image/") and mime not in VISION_IMAGE_MIME_TYPES:
            return self._ok(message, f"'{item.path}' is in a format that cannot be viewed yet ({mime}).", [item])
        vision_cap = (MAX_DRIVE_VISION_IMAGE_BYTES if mime.startswith("image/")
                      else MAX_DRIVE_VISION_PDF_BYTES if mime == "application/pdf" else None)
        try:
            if vision_cap is not None:
                if item.size_bytes > vision_cap:
                    raise DriveFileTooLargeError(item, vision_cap)
                data = await self._conversion_service.resolve_bytes(ref, user_id)
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(item.name)[1] or ".bin")
                os.close(tmp_fd)
                async with aiofiles.open(tmp_path, "wb") as f:
                    await f.write(data)
                return self._ok(message, f"{drive_label(item)} is attached. You can see and analyse it directly.",
                                [item], metadata={"file_data": {"path": tmp_path, "mime_type": mime}})
            if mime.startswith("video/"):
                data = await self._conversion_service.resolve_bytes(ref, user_id)
                if not self._notification:
                    return self._fail(message, "File delivery is not configured — cannot send the video.")
                await self._notification.notify_file_bytes(
                    user_id=user_id, account_id=message.context.get("account_id") or "",
                    file_bytes=data, filename=item.name, title="Video",
                    channel_id_override=message.context.get("origin_channel_id"),
                    platform_override=message.context.get("origin_platform"),
                )
                return self._ok(message, f"{drive_label(item)} has been sent to the user as a file.", [item])
        except DriveFileTooLargeError as e:
            return self._ok(message, f"'{item.path}' is {format_size(item.size_bytes)}; files above "
                                     f"{format_size(e.limit_bytes)} cannot be opened yet.", [item])
        content = await self._conversion_service.resolve_content(ref, user_id)
        return self._ok(message, f"{drive_label(item)}\n{content}", [item])

    async def _save_to_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        ref = payload.get("file_ref") or ""
        if not ref:
            return self._fail(message, "file_ref is required: the ref from a [File: ...] label.")
        if is_drive_ref(ref):
            return self._fail(message, f"'{ref}' is already on the drive; use move_file_in_drive.")
        data = await self._conversion_service.resolve_bytes(ref, user_id)
        filename = drive_filename(payload.get("name"), ref)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        assert self._drive is not None
        out = await self._drive.save(user_id, data, filename, mime, payload.get("folder") or None)
        created = f" Created folders: {', '.join(out.created)}." if out.created else ""
        if out.already_existed:
            head = f"Already on the drive: {drive_label(out.item)}."
        elif out.renamed:
            head = f'Saved as "{out.item.name}" (the name "{filename}" was taken): {drive_label(out.item)}.'
        else:
            head = f"Saved {drive_label(out.item)}."
        return self._ok(message, head + created, [out.item])

    async def _list_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        assert self._drive is not None
        out = await self._drive.list_folder(user_id, payload.get("folder") or None)
        lines = [drive_label(out.folder)] + [f"  {drive_label(i)}" for i in out.items]
        if not out.items:
            lines.append("  (empty)")
        if out.truncated:
            lines.append("  … more items not shown; list a subfolder.")
        return self._ok(message, "\n".join(lines), [out.folder, *out.items])

    async def _search_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        query = (payload.get("search_text") or "").strip()
        if not query:
            return self._fail(message, "search_text is required.")
        assert self._drive is not None
        hits = await self._drive.search(user_id, query)
        if not hits:
            return self._ok(message, f"No files on the drive matched '{query}'.", [])
        return self._ok(message, "\n".join(drive_label(i) for i in hits), hits)

    async def _move_in_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        item_id = parse_drive_ref(payload.get("file_ref") or "")
        if item_id is None:
            return self._wrong_store(message, payload.get("file_ref") or "")
        assert self._drive is not None
        out = await self._drive.move(user_id, item_id, payload.get("folder") or None, payload.get("new_name") or None)
        created = f" Created folders: {', '.join(out.created)}." if out.created else ""
        return self._ok(message, f"Moved: {out.before.path} → {drive_label(out.after)}.{created}", [out.after])

    async def _create_folder_in_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        assert self._drive is not None
        out = await self._drive.ensure_folder(user_id, payload.get("folder") or "")
        created = f" Created: {', '.join(out.created)}." if out.created else " It already existed."
        return self._ok(message, f"{drive_label(out.folder)}{created}", [out.folder])

    async def _update_in_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        item_id = parse_drive_ref(payload.get("file_ref") or "")
        if item_id is None:
            return self._wrong_store(message, payload.get("file_ref") or "")
        append_text, source_ref = payload.get("append_text"), payload.get("source_ref")
        if bool(append_text) == bool(source_ref):
            return self._fail(message, 'Give exactly one of "append_text" or "source_ref".')
        assert self._drive is not None
        if append_text:
            out = await self._drive.append_text(user_id, item_id, append_text)
            return self._ok(message, f"Appended to {drive_label(out.item)}.", [out.item])
        data = await self._conversion_service.resolve_bytes(source_ref, user_id)
        target = await self._drive.get_item(user_id, item_id)
        mime = target.mime_type or (mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        out = await self._drive.replace_with(user_id, item_id, data, mime)
        await self._receipt(message, user_id, UIMessage.DRIVE_REPLACED, path=out.item.path,
                            before=format_size(out.before_size), after=format_size(out.item.size_bytes))
        return self._ok(message, f"Replaced {drive_label(out.item)}.", [out.item])

    async def _delete_from_drive(self, message: AgentMessage, payload: dict, user_id: str) -> AgentResponse:
        item_id = parse_drive_ref(payload.get("file_ref") or "")
        if item_id is None:
            return self._wrong_store(message, payload.get("file_ref") or "")
        assert self._drive is not None
        out = await self._drive.delete(user_id, item_id)
        if out.item.is_folder:
            count = f"{out.file_count}+" if out.count_capped else str(out.file_count)
            await self._receipt(message, user_id, UIMessage.DRIVE_DELETED_FOLDER, path=out.item.path, count=count)
            return self._ok(message, f"Deleted folder {out.item.path} ({count} files).", [])
        await self._receipt(message, user_id, UIMessage.DRIVE_DELETED_FILE, path=out.item.path)
        return self._ok(message, f"Deleted {out.item.path}.", [])

    async def _receipt(self, message: AgentMessage, user_id: str, ui: UIMessage, **fmt: str) -> None:
        """Deterministic chat receipt of a destructive action (§4.10). Best effort, logged on failure."""
        if not (self._notification and self._localization):
            logger.warning("FileManagementAgent: drive receipt not wired (%s)", ui.value)
            return
        try:
            lang = await self._language.resolve_ui_language(user_id) if self._language else LanguageCode.EN
        except Exception:
            logger.warning("FileManagementAgent: UI language lookup failed for %s", user_id[:8], exc_info=True)
            lang = LanguageCode.EN
        try:
            text = self._localization.get_ui_string(lang, ui).format(**fmt)
            await self._notification.notify_raw(
                user_id, message.context.get("account_id") or "", text,
                channel_id_override=message.context.get("origin_channel_id"),
                platform_override=message.context.get("origin_platform"),
            )
        except Exception:
            logger.warning("FileManagementAgent: drive receipt failed (%s)", ui.value, exc_info=True)
```

`format_size(2048) == "2KB"`, `format_size(3000) == "3KB"` — matches `test_replace_receipt_with_sizes`.

- [ ] **Step 5:** `pytest tests/unit/agents/test_file_management_agent_drive.py tests/unit/agents/test_file_management_agent.py tests/unit/agents/test_file_management_agent_skill_refs.py tests/unit/test_req_ui_06_localization.py -v && ruff check src/agents/file_management_agent.py` → PASS. Existing failures → reviewer.

- [ ] **Step 6: Commit** — `git add src/agents/file_management_agent.py src/domain/ui_messages.py src/locales/*.py tests/unit/agents/test_file_management_agent_drive.py && git commit -m "feat(drive): drive intents in FileManagementAgent — store guards, shielded mutations, receipts"`

---

## Task 9: Wiring — config, container, factory (timeout 120 s), OAuth connect, Cabinet card, deploy secret

**Files:**
- Modify: `src/config/settings.py` (next to `MICROSOFT_TODO_REDIRECT_URI`, ~line 88), `.env.example`, `cloudbuild-dev.yaml`
- Modify: `src/composition/service_container.py` (after the To Do block ~line 150; `FileConversionService(` ~line 308; `agent_services()` ~line 349)
- Modify: `src/composition/user_agent_factory.py` (constructor; `_build_file_management` ~line 819)
- Modify: `main.py` (`UserAgentFactory(` ~line 488; `create_oauth_blueprint(` ~line 601; `create_user_cabinet_blueprint(` call)
- Modify: `src/web/oauth_app.py` (signature ~line 45; routes after the To Do callback)
- Modify: `src/web/user_cabinet_app.py` (param + two routes), `src/web/static/cabinet.html`
- Test: `tests/unit/web/test_oauth_app_onedrive.py`, `tests/unit/web/test_user_cabinet_app_drive.py`, `tests/unit/composition/test_drive_wiring.py`

**Interfaces — produces:** config `ONEDRIVE_REDIRECT_URI`; container `user_drive: Optional[UserDrivePort]`, `user_drive_service: Optional[UserDriveService]`; routes `GET /auth/connect-onedrive`, `GET /auth/connect-onedrive/callback`, `GET /api/drive/status` → `{"connected": bool, "provider": str}`, `DELETE /api/drive/disconnect` → `{"success": true}`.

- [ ] **Step 1: Failing tests**

`tests/unit/web/test_oauth_app_onedrive.py`:

```python
"""Drive connect routes (docs/10_rfcs/USER_DRIVE_RFC.md §4.1)."""
import inspect
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from quart import Quart

from src.adapters.onedrive_adapter import ONEDRIVE_PROVIDER
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.web import oauth_app
from src.web.oauth_app import create_oauth_blueprint


def _resp(json_data, status=200):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=str(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(post):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.post.return_value = post
    return s


@pytest.fixture
def oauth_port():
    return AsyncMock(spec=OAuthCredentialsPort)


def _app(oauth_port, redirect="https://example.test/auth/connect-onedrive/callback"):
    session_service = Mock()
    session_service.verify_access_token = Mock(return_value={"sub": "u1"})
    auth_config = Mock()
    auth_config.oauth_redirect_uri = "https://example.test/auth/callback"
    app = Quart("test_drive_oauth")
    app.register_blueprint(create_oauth_blueprint(
        auth_service=Mock(), session_service=session_service, auth_registry=Mock(),
        auth_config=auth_config, oauth_credentials_port=oauth_port,
        ms_todo_client_id="cid", ms_todo_client_secret="csecret", onedrive_redirect_uri=redirect,
    ))
    return app


async def test_connect_redirects_with_app_folder_scope(oauth_port):
    async with _app(oauth_port).test_client() as client:
        client.set_cookie("localhost", "access_token", "valid")
        resp = await client.get("/auth/connect-onedrive")
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "login.microsoftonline.com/consumers/oauth2/v2.0/authorize" in location
    assert "Files.ReadWrite.AppFolder" in location and "offline_access" in location


async def test_callback_saves_credentials_under_drive_provider(oauth_port):
    token = _resp({"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                   "scope": "Files.ReadWrite.AppFolder offline_access"})
    async with _app(oauth_port).test_client() as client:
        client.set_cookie("localhost", "drive_oauth_state", "s1")
        client.set_cookie("localhost", "drive_connect_user_id", "u1")
        with patch("aiohttp.ClientSession", return_value=_session(token)):
            resp = await client.get("/auth/connect-onedrive/callback?code=c&state=s1")
    assert resp.headers["Location"] == "/cabinet?drive_connected=1"
    saved = oauth_port.save_credentials.call_args.args[0]
    assert saved.provider == ONEDRIVE_PROVIDER and saved.user_id == "u1" and saved.refresh_token == "r"


async def test_callback_state_mismatch(oauth_port):
    async with _app(oauth_port).test_client() as client:
        client.set_cookie("localhost", "drive_oauth_state", "s1")
        client.set_cookie("localhost", "drive_connect_user_id", "u1")
        resp = await client.get("/auth/connect-onedrive/callback?code=c&state=evil")
    assert resp.headers["Location"] == "/cabinet?drive_error=state"
    oauth_port.save_credentials.assert_not_called()


async def test_501_when_not_configured(oauth_port):
    async with _app(oauth_port, redirect="").test_client() as client:
        client.set_cookie("localhost", "access_token", "valid")
        resp = await client.get("/auth/connect-onedrive")
    assert resp.status_code == 501


def test_provider_key_matches_adapter():
    assert f'"{ONEDRIVE_PROVIDER}"' in inspect.getsource(oauth_app)
```

`tests/unit/web/test_user_cabinet_app_drive.py`:

```python
"""Drive status/disconnect in the Cabinet (docs/10_rfcs/USER_DRIVE_RFC.md §4.1)."""
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.ports.user_drive_port import UserDrivePort
from src.web.user_cabinet_app import create_user_cabinet_blueprint

_AUTH = {"Authorization": "Bearer token"}


def _app(user_drive):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_drive_cabinet")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(),
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive=user_drive,
    ))
    return app


def _drive(connected=True):
    drive = MagicMock(spec=UserDrivePort)
    drive.is_connected = AsyncMock(return_value=connected)
    drive.disconnect = AsyncMock()
    drive.display_name = "OneDrive"
    return drive


class TestDriveCabinet:
    async def test_status_connected(self):
        async with _app(_drive()).test_client() as client:
            resp = await client.get("/api/drive/status", headers=_AUTH)
        assert (await resp.get_json()) == {"connected": True, "provider": "OneDrive"}

    async def test_status_without_drive_wired(self):
        async with _app(None).test_client() as client:
            resp = await client.get("/api/drive/status", headers=_AUTH)
        assert (await resp.get_json()) == {"connected": False, "provider": ""}

    async def test_disconnect(self):
        drive = _drive()
        async with _app(drive).test_client() as client:
            resp = await client.delete("/api/drive/disconnect", headers=_AUTH)
        assert resp.status_code == 200
        drive.disconnect.assert_awaited_once_with("u1")
```

`tests/unit/composition/test_drive_wiring.py`:

```python
"""The drive reaches FileManagementAgent only through composition, with the drive timeout."""
import inspect

from src.agents.file_management_agent import FileManagementAgent
from src.composition import user_agent_factory


def test_factory_wires_drive_and_timeout():
    src = inspect.getsource(user_agent_factory.UserAgentFactory._build_file_management)
    for kw in ("drive_service=", "localization=", "language_service=", "timeout_ms=120_000"):
        assert kw in src


def test_agent_accepts_drive_kwargs():
    params = inspect.signature(FileManagementAgent.__init__).parameters
    assert {"drive_service", "localization", "language_service"} <= set(params)
```

- [ ] **Step 2:** `pytest tests/unit/web/test_oauth_app_onedrive.py tests/unit/web/test_user_cabinet_app_drive.py tests/unit/composition/test_drive_wiring.py -v` → FAIL.

- [ ] **Step 3: Config + deploy secret**

`src/config/settings.py`: `"ONEDRIVE_REDIRECT_URI": os.getenv("ONEDRIVE_REDIRECT_URI", ""),` with comment `# User drive — OneDrive App Folder (USER_DRIVE_RFC §4.1); same app registration as To Do`. `.env.example`: `ONEDRIVE_REDIRECT_URI=` under `MICROSOFT_TODO_REDIRECT_URI=`. `cloudbuild-dev.yaml`: append `,ONEDRIVE_REDIRECT_URI=ONEDRIVE_REDIRECT_URI_DEV:latest` right after `MICROSOFT_TODO_REDIRECT_URI=MICROSOFT_TODO_REDIRECT_URI_DEV:latest` inside `--set-secrets`. Run `pytest tests/unit/test_arch_config_key_registration.py -v` → PASS.

- [ ] **Step 4: Container + factory + main**

`service_container.py`, right after the To Do block (it precedes `FileConversionService(` — verify with `grep -n "ms_todo_adapter\|FileConversionService(" src/composition/service_container.py`):

```python
        # User drive — OneDrive App Folder (USER_DRIVE_RFC §4.2). Same app registration as To Do;
        # functional only once the user has connected it in the Cabinet.
        self.user_drive: Optional[UserDrivePort] = None
        self.user_drive_service: Optional[UserDriveService] = None
        if config.get("MICROSOFT_TODO_CLIENT_ID") and config.get("MICROSOFT_TODO_CLIENT_SECRET"):
            self.user_drive = OneDriveAdapter(
                oauth_credentials=self.oauth_credentials,
                client_id=config["MICROSOFT_TODO_CLIENT_ID"],
                client_secret=config["MICROSOFT_TODO_CLIENT_SECRET"],
            )
            self.user_drive_service = UserDriveService(self.user_drive)
```

Pass `drive=self.user_drive` into `FileConversionService(...)`; add `"user_drive_service": self.user_drive_service` to `agent_services()`.

`user_agent_factory.py`: constructor kwargs `user_drive_service: Optional[UserDriveService] = None`, `localization: Optional[LocalizationService] = None`, `language_service: Optional[LanguageServicePort] = None` → `self.user_drive_service`, `self.localization`, `self.language_service`; `_build_file_management`: `timeout_ms=120_000,  # drive operations (USER_DRIVE_RFC §4.13)` and `drive_service=self.user_drive_service, localization=self.localization, language_service=self.language_service`.

`main.py` `UserAgentFactory(...)`: add `localization=_localization, language_service=_language_service` (both exist before the factory, lines ~304–318). The drive service arrives via `**container.agent_services()`; if main passes services explicitly instead, add `user_drive_service=container.user_drive_service`.

- [ ] **Step 5: OAuth routes** — `create_oauth_blueprint(..., onedrive_redirect_uri: Optional[str] = None, ...)`; after the To Do callback:

```python
    # ========================================================================
    # GET /auth/connect-onedrive — the user's drive (USER_DRIVE_RFC §4.1)
    # ========================================================================
    _ONEDRIVE_PROVIDER = "microsoft_onedrive"  # == adapters.onedrive_adapter.ONEDRIVE_PROVIDER (tested)
    _ONEDRIVE_SCOPE = "Files.ReadWrite.AppFolder offline_access"

    @bp.route("/auth/connect-onedrive", methods=["GET"])
    async def connect_onedrive():
        if not ms_todo_client_id or not onedrive_redirect_uri:
            return jsonify({"error": "Drive integration not configured"}), 501
        access_token = request.cookies.get("access_token")
        if not access_token:
            return redirect("/auth/login?next=/cabinet")
        try:
            user_id = session_service.verify_access_token(access_token)["sub"]
        except jwt.InvalidTokenError:
            return redirect("/auth/login?next=/cabinet")
        state = secrets.token_urlsafe(32)
        from urllib.parse import urlencode
        auth_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?" + urlencode({
            "client_id": ms_todo_client_id, "response_type": "code", "redirect_uri": onedrive_redirect_uri,
            "scope": _ONEDRIVE_SCOPE, "state": state, "response_mode": "query",
        })
        logger.info(f"📁 Drive OAuth initiated for user={user_id[:8]}")
        response = await make_response(redirect(auth_url))
        response.set_cookie("drive_oauth_state", state, max_age=600, httponly=True, secure=True, samesite="lax")
        response.set_cookie("drive_connect_user_id", user_id, max_age=600, httponly=True, secure=True, samesite="lax")
        return response

    @bp.route("/auth/connect-onedrive/callback", methods=["GET"])
    async def connect_onedrive_callback():
        if not ms_todo_client_id or not ms_todo_client_secret or not onedrive_redirect_uri:
            return jsonify({"error": "Drive integration not configured"}), 501
        if request.args.get("error"):
            return redirect("/cabinet?drive_error=denied")
        state, code = request.args.get("state"), request.args.get("code")
        user_id = request.cookies.get("drive_connect_user_id")
        if not state or state != request.cookies.get("drive_oauth_state") or not user_id:
            logger.warning("⚠️ Drive OAuth callback CSRF validation failed")
            return redirect("/cabinet?drive_error=state")
        if not code:
            return redirect("/cabinet?drive_error=no_code")
        try:
            import aiohttp
            from datetime import datetime, timedelta, timezone
            from ..domain.email import OAuthCredentials
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                    data={"client_id": ms_todo_client_id, "client_secret": ms_todo_client_secret,
                          "code": code, "redirect_uri": onedrive_redirect_uri,
                          "grant_type": "authorization_code", "scope": _ONEDRIVE_SCOPE},
                ) as resp:
                    if not resp.ok:
                        raise RuntimeError(f"Token exchange failed: {resp.status} {await resp.text()}")
                    token_data = await resp.json()
            await oauth_credentials_port.save_credentials(OAuthCredentials(
                user_id=user_id, provider=_ONEDRIVE_PROVIDER,
                access_token=token_data["access_token"], refresh_token=token_data.get("refresh_token"),
                token_expiry=datetime.now(timezone.utc) + timedelta(seconds=int(token_data.get("expires_in", 3600))),
                scopes=token_data.get("scope", "").split(), email_address="",
            ))
            logger.info(f"✅ Drive connected for user={user_id[:8]}")
        except Exception as exc:
            logger.error(f"💥 Drive OAuth callback failed: {exc}", exc_info=True)
            err = await make_response(redirect("/cabinet?drive_error=exchange"))
            err.delete_cookie("drive_oauth_state")
            err.delete_cookie("drive_connect_user_id")
            return err
        response = await make_response(redirect("/cabinet?drive_connected=1"))
        response.delete_cookie("drive_oauth_state")
        response.delete_cookie("drive_connect_user_id")
        return response
```

`main.py` `create_oauth_blueprint(...)`: `onedrive_redirect_uri=config.get("ONEDRIVE_REDIRECT_URI", "")`.

- [ ] **Step 6: Cabinet** — `create_user_cabinet_blueprint(..., user_drive: Optional[UserDrivePort] = None)` and:

```python
    @bp.route("/api/drive/status", methods=["GET"])
    @auth_required
    async def drive_status():
        """The user's drive connection (USER_DRIVE_RFC §4.1)."""
        if not user_drive:
            return jsonify({"connected": False, "provider": ""}), 200
        try:
            return jsonify({"connected": await user_drive.is_connected(g.user_id),
                            "provider": user_drive.display_name}), 200
        except Exception as exc:
            logger.error(f"Error fetching drive status: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/drive/disconnect", methods=["DELETE"])
    @auth_required
    async def drive_disconnect():
        if not user_drive:
            return jsonify({"error": "Drive integration not configured"}), 501
        try:
            await user_drive.disconnect(g.user_id)
            logger.info(f"🔌 Drive disconnected for user={g.user_id[:8]}")
            return jsonify({"success": True}), 200
        except Exception as exc:
            logger.error(f"Error disconnecting drive: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
```

`main.py`: pass `user_drive=container.user_drive` to `create_user_cabinet_blueprint(...)`.

`cabinet.html`: `<div id="drive-container"></div>` next to `tasks-container`; `loadDriveStatus()` called wherever `loadTasksStatus()` is; `renderDriveStatus(data)` modelled on `renderServiceStatus` (title `data.provider || t("platform.drive_title")`, description `t("platform.drive_desc")`, connect link `/auth/connect-onedrive`, disconnect via `DELETE /api/drive/disconnect` after `confirm()`); `?drive_connected=1` / `?drive_error` toasts next to the To Do ones (~line 2227). i18n keys in all four dictionaries:

```
"platform.drive_title":   EN "Drive" | UK "Диск" | FR "Disque" | ES "Disco"
"platform.drive_desc":    EN "Long-term file area: files you ask me to remember live in the bot's own folder."
                          UK "Довготривала зона файлів: файли, які ти просиш запам'ятати, лежать у власній папці бота."
                          FR "Espace de fichiers à long terme : les fichiers que tu me demandes de retenir vivent dans le dossier du bot."
                          ES "Área de archivos a largo plazo: los archivos que me pides recordar viven en la carpeta del bot."
"platform.connect_drive": EN "Connect drive" | UK "Підключити диск" | FR "Connecter le disque" | ES "Conectar disco"
```

- [ ] **Step 7:** `pytest tests/unit/web/ tests/unit/composition/ tests/unit/test_arch_config_key_registration.py -v` → PASS.

- [ ] **Step 8: Commit** — `git add src/config/settings.py .env.example cloudbuild-dev.yaml src/composition/ main.py src/web/ tests/unit/web/test_oauth_app_onedrive.py tests/unit/web/test_user_cabinet_app_drive.py tests/unit/composition/test_drive_wiring.py && git commit -m "feat(drive): wire the drive — OAuth connect, Cabinet card, 120 s agent timeout"`

---

## Task 10: Consolidation — file operations stay out of long-term memory

**Files:**
- Create: `scripts/consolidation/test_file_ops_exclusion_dryrun.py`, `scripts/prompt/migrate_file_ops_exclusion_token.py`

**Interfaces — consumes** `run_stage1(agent, messages, bio_facts, user_id, account_id, use_new_rule) -> (operations, elapsed, tokens, patched_hit)` from `scripts/consolidation/test_stage1_classification_dryrun.py`. It reads the module-level `patch_prompt` at call time; messages are `{"role", "text", "timestamp"}` (the shape its `load_window` returns); a CREATE op holds the fact in `op["content"]`, UPDATE in `op["updates"]`, MERGE in `op["content"]`. Agent construction is copied from that script's `main()`. The base script is not modified.

The line (§4.12), appended as the last element of `exclude: [` in `rule Trivial_Exclusions()` of `CONSOLIDATION_TAXONOMY`:

```
"File operations: saving, opening, moving, renaming, deleting files or folders on the user's drive or in chat — the drive is the record of what exists and where"
```

- [ ] **Step 1: Dry-run script**

```python
#!/usr/bin/env python3
"""
Stage-1 dry run: do file-operation exchanges leak into long-term memory?
(docs/10_rfcs/USER_DRIVE_RFC.md §4.12)

Real Stage-1 consolidation, fact writes intercepted, on a SYNTHETIC batch of drive
exchanges plus one real fact. `--rule new` adds the file-operations line to
Trivial_Exclusions in the ASSEMBLED prompt only — Firestore is untouched.

A leak is any written fact that mentions a file location or operation — including a
mixed one ("lease ends Dec 2027, contract in Договоры/"), the likeliest leak.

    python scripts/consolidation/test_file_ops_exclusion_dryrun.py --rule both --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from google.cloud import firestore  # noqa: E402

from scripts.consolidation import test_stage1_classification_dryrun as base  # noqa: E402
from src.adapters.firestore_account_repo import FirestoreAccountRepository  # noqa: E402
from src.adapters.firestore_user_repo import FirestoreUserRepository  # noqa: E402
from src.composition.service_container import ServiceContainer  # noqa: E402
from src.composition.user_agent_factory import UserAgentFactory  # noqa: E402
from src.config.settings import load_settings  # noqa: E402
from src.infrastructure.agent_coordinator import AgentCoordinator  # noqa: E402

_LINE = ('"File operations: saving, opening, moving, renaming, deleting files or folders on the '
         'user\'s drive or in chat — the drive is the record of what exists and where"')
_EXCLUDE_RE = re.compile(r'("Temporary debugging state: [^"]*")(\s*\n\s*\])')


def patch_prompt(prompt: str):
    patched, n = _EXCLUDE_RE.subn(lambda m: f"{m.group(1)},\n                {_LINE}{m.group(2)}", prompt, count=1)
    return patched, bool(n)


def _m(role: str, text: str) -> Dict[str, Any]:
    return {"role": role, "text": text, "timestamp": time.time()}


BATCH = [
    _m("user", 'Запомни этот файл [File: "lease_2026.pdf" (1.2MB)]'),
    _m("model", "Saved [Drive: Inbox/lease_2026.pdf (1.2MB)] to Inbox."),
    _m("user", "Перенеси его в Договоры/Аренда"),
    _m("model", "Moved: Inbox/lease_2026.pdf → Договоры/Аренда/lease_2026.pdf."),
    _m("user", "Удали папку Встречи/2025"),
    _m("model", "Deleted folder Встречи/2025 (14 files)."),
    _m("user", "Кстати, аренда квартиры заканчивается в декабре 2027"),
    _m("model", "Noted: the lease ends in December 2027."),
]
_FILE_TERMS = re.compile(
    r"inbox|договор[ыи]/|аренда/|встречи/|lease_2026|\.pdf\b|\bdrive\b|диск|папк|folder|"
    r"\bsaved\b|\bmoved\b|\bdeleted\b|сохран|перен[её]с|удал", re.I)
_REAL_FACT = re.compile(r"2027")


def _op_text(op: Dict[str, Any]) -> str:
    return str(op.get("content") or json.dumps(op.get("updates") or {}, ensure_ascii=False))


async def _build_agent(user_id: str):
    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    cfg = load_settings()
    env = cfg["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env.account_collection_name)
    user_repo = FirestoreUserRepository(db, env, account_repo)
    container = ServiceContainer(config=cfg, db_client=db, env_config=env, account_repo=account_repo)
    factory = UserAgentFactory(config=cfg, env_config=env, coordinator=AgentCoordinator(),
                               user_repo=user_repo, account_repo=account_repo, **container.agent_services())
    agent = (await factory.ensure_agents_for_user(user_id)).get("consolidation_agent")
    if agent is None or agent._fact_management is None:
        raise SystemExit("consolidation_agent unavailable")

    async def _noop_async(*a, **k):
        pass

    agent._repo.refresh_biographical_context_cache = _noop_async
    if agent.prompt_builder:
        agent.prompt_builder.invalidate_biographical_cache = lambda *a, **k: None
    return agent


async def main(rule: str, runs: int) -> None:
    base.patch_prompt = patch_prompt
    user_id, account_id = os.environ["DEV_USER_ID"], os.environ["DEV_ACCOUNT_ID"]
    agent = await _build_agent(user_id)
    print(f"model: {agent.model_name}")
    for leg in (["baseline", "new"] if rule == "both" else [rule]):
        for i in range(runs):
            ops, elapsed, _, hit = await base.run_stage1(agent, BATCH, [], user_id, account_id, leg == "new")
            if leg == "new" and not hit:
                raise SystemExit("Trivial_Exclusions anchor NOT matched — fix _EXCLUDE_RE to the live token text.")
            texts = [_op_text(op) for op in ops if op.get("action") in ("CREATE", "UPDATE", "MERGE")]
            leaks = [t for t in texts if _FILE_TERMS.search(t)]
            clean_real = any(_REAL_FACT.search(t) and not _FILE_TERMS.search(t) for t in texts)
            print(f"[{leg} run {i + 1}] {elapsed:.0f}s ops={len(ops)} leaks={len(leaks)} "
                  f"real_fact_clean={clean_real}")
            for t in leaks:
                print(f"    LEAK: {t[:160]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", choices=["baseline", "new", "both"], default="both")
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    asyncio.run(main(a.rule, a.runs))
```

- [ ] **Step 2: Anchor smoke check** — `python scripts/consolidation/test_file_ops_exclusion_dryrun.py --rule new --runs 1`. On "anchor NOT matched": print the assembled `Trivial_Exclusions` block from a local throwaway copy of this script (not by editing the base script) and fix `_EXCLUDE_RE`.

- [ ] **Step 3: Run** — `python scripts/consolidation/test_file_ops_exclusion_dryrun.py --rule both --runs 3`. Record in RFC §4.12 (dated): leaks per run for baseline and new, `real_fact_clean` per run. The line is added either way; zero baseline leaks → recorded as preventive.

- [ ] **Step 4: Token migration script** — model on `scripts/prompt/migrate_reminder_rrule_tokens.py` (same `--dry-run` / `--apply` / `--revert <backup>`, backup to `scripts/memory/`, exact-anchor abort, collection `f"{env.domain_prompt_tokens_collection}_system"`), one edit on `CONSOLIDATION_TAXONOMY`:

```python
_ANCHOR_OLD = '''                "Temporary debugging state: 'Testing feature X' (unless ongoing project)"
            ]'''
_ANCHOR_NEW = '''                "Temporary debugging state: 'Testing feature X' (unless ongoing project)",
                "File operations: saving, opening, moving, renaming, deleting files or folders on the user's drive or in chat — the drive is the record of what exists and where"
            ]'''
```

Abort unless `_ANCHOR_OLD` occurs exactly once.

- [ ] **Step 5: Owner approval** — `--dry-run`, show the diff and the Step 3 numbers to the owner; `--apply` only on explicit approval (live prompt data).

- [ ] **Step 6: Commit** — `git add scripts/consolidation/test_file_ops_exclusion_dryrun.py scripts/prompt/migrate_file_ops_exclusion_token.py docs/10_rfcs/USER_DRIVE_RFC.md && git commit -m "feat(drive): keep file operations out of consolidation (dry run + token migration)"`

---

## Task 11: Routing probe on the live Smart (single- and two-turn)

**Files:**
- Create: `scripts/validation/drive_routing_probe.py`

**Interfaces — consumes** the Smart harness of `scripts/validation/ab_agent_models.py` (container + `AgentRegistry` + factory; `DelegationEngine.dispatch` swap; `AgentMessage` with `current_message_parts`; agent key `smart_agent`). Two-turn scenarios seed the previous exchange by wrapping `smart._load_conversation_context` (`smart_response_agent.py:654`): the wrapper returns `SEED + await original(...)`.

- [ ] **Step 1: Probe**

```python
#!/usr/bin/env python3
"""
Routing probe for the drive intents (docs/10_rfcs/USER_DRIVE_RFC.md §7).
Runs the REAL Smart on fixed phrasings; every delegation is captured and answered with
a stub, nothing executes. Two-turn scenarios seed the previous exchange into Smart's
loaded history. Each scenario runs --runs times.

    python scripts/validation/drive_routing_probe.py --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from google.cloud import firestore  # noqa: E402

from src.adapters.firestore_account_repo import FirestoreAccountRepository  # noqa: E402
from src.adapters.firestore_user_repo import FirestoreUserRepository  # noqa: E402
from src.composition.service_container import ServiceContainer  # noqa: E402
from src.composition.user_agent_factory import UserAgentFactory  # noqa: E402
from src.config.settings import load_settings  # noqa: E402
from src.domain.agent import AgentIntent, AgentMessage  # noqa: E402
from src.domain.llm import Message, MessagePart  # noqa: E402
from src.domain.request_context import RequestContext  # noqa: E402
from src.infrastructure.agent_coordinator import AgentCoordinator  # noqa: E402
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS  # noqa: E402
from src.infrastructure.agent_registry import AgentRegistry  # noqa: E402
from src.infrastructure.delegation_engine import DelegationEngine, ToolResult  # noqa: E402

LEASE = '[File: "lease_2026.pdf" (1.2MB)]'
SEARCH_RESULT = ("[Drive: Договоры/lease_2026.pdf (1.2MB) ref=drive:AAA]\n"
                 "[Drive: Договоры/lease_2025.pdf (1.1MB) ref=drive:BBB]")


def _pair(user: str, model: str, drive_context: Any = None) -> List[Message]:
    """A stored exchange in production shape: refs live only in the `*_context` JSON block that
    ConversationHandler appends to the model turn (conversation_handler.py:1326-1340), never in the
    prose — the reply is not assumed to repeat any label."""
    model_text = model
    if drive_context is not None:
        model_text += "\n\n" + json.dumps({"drive_context": drive_context}, ensure_ascii=False, separators=(",", ":"))
    return [Message(role="user", parts=[MessagePart(text=user)]),
            Message(role="model", parts=[MessagePart(text=model_text)])]


_FOUND = [[{"path": "Договоры/lease_2026.pdf", "ref": "drive:AAA"},
           {"path": "Договоры/lease_2025.pdf", "ref": "drive:BBB"}]]

SCENARIOS: List[Dict[str, Any]] = [
    {"text": f"Запомни этот файл {LEASE}", "expect": "save_file_to_drive", "folder": None},
    {"text": "Запомни, что аренда заканчивается в декабре 2027", "expect": "save_to_memory"},
    {"text": f"Запомни этот файл в папку Встречи {LEASE}", "expect": "save_file_to_drive", "folder": "Встречи"},
    {"text": "Найди на диске договор аренды", "expect": "search_files_in_drive"},
    {"seed": _pair(f"Открой {LEASE}", "Lease agreement for the flat, 12 months, ends Dec 2027."),
     "text": "Положи его в папку Договоры", "expect": "save_file_to_drive", "forbid": "move_file_in_drive"},
    {"seed": _pair("Найди договоры аренды", "Нашёл два договора: на 2026 и на 2025 год.", _FOUND),
     "text": "Открой второй", "expect": "open_file_from_drive", "ref": "drive:BBB"},
]
_STUB = {"search_files_in_drive": SEARCH_RESULT,
         "open_file": "[File: lease_2026.pdf]\nLease agreement…\n[/File: lease_2026.pdf]"}
CALLS: List[Dict[str, Any]] = []


def _install_capture() -> None:
    async def capture(self, tool_call, context, *args, **kwargs):
        a = tool_call.args or {}
        CALLS.append({"intent": a.get("intent", ""), "context": a.get("context") or {}})
        return ToolResult(name=tool_call.name, result_str=_STUB.get(a.get("intent", ""), "Done."))
    DelegationEngine.dispatch = capture


async def main(runs: int) -> int:
    user_id, account_id = os.environ["DEV_USER_ID"], os.environ["DEV_ACCOUNT_ID"]
    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    settings = load_settings()
    env_config = settings["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    container = ServiceContainer(config=settings, db_client=db, env_config=env_config, account_repo=account_repo)
    registry = AgentRegistry()
    for d in ALL_DESCRIPTORS:
        registry.register(d)
    factory = UserAgentFactory(config=settings, env_config=env_config, coordinator=AgentCoordinator(registry=registry),
                               user_repo=user_repo, account_repo=account_repo, **container.agent_services())
    smart = (await factory.ensure_agents_for_user(user_id))["smart_agent"]
    original_load = smart._load_conversation_context
    _install_capture()
    failures = 0
    for sc in SCENARIOS:
        seed = sc.get("seed", [])

        async def seeded(*args, _seed=seed, **kwargs):
            return list(_seed) + await original_load(*args, **kwargs)

        smart._load_conversation_context = seeded
        for run in range(runs):
            CALLS.clear()
            msg = AgentMessage.create(
                sender="drive_routing_probe", recipient=smart.agent_id, intent=AgentIntent.QUERY,
                payload={"text": sc["text"]},
                context={"user_id": user_id, "account_id": account_id,
                         "session_id": f"{user_id}:drive_routing_probe",
                         "current_message_parts": [MessagePart(text=sc["text"])]},
            )
            async with RequestContext(user_id=user_id, account_id=account_id):
                await smart.execute(msg)
            intents = [c["intent"] for c in CALLS]
            ok = sc["expect"] in intents and sc.get("forbid") not in intents
            hit = next((c for c in CALLS if c["intent"] == sc["expect"]), None)
            if ok and "folder" in sc:
                got = hit["context"].get("folder") or None
                ok = (got is None) if sc["folder"] is None else (got or "").casefold() == sc["folder"].casefold()
            if ok and "ref" in sc:
                ok = sc["ref"] in str(hit["context"].get("file_ref", ""))
            failures += 0 if ok else 1
            print(f"{'OK ' if ok else 'BAD'} [{run + 1}] {sc['text'][:50]!r} → {intents}")
    smart._load_conversation_context = original_load
    print(f"\n{failures} failing run(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    sys.exit(asyncio.run(main(ap.parse_args().runs)))
```

Before running, check `Message(role=..., parts=[...])` against `src/domain/llm.py` (required fields, role values) and the `ToolResult` import against `ab_agent_models.py`.

- [ ] **Step 2: Run** — `python scripts/validation/drive_routing_probe.py --runs 3` → `0 failing run(s)`. On failures, change **only** the capability descriptions in `agent_manifest.py` (re-run Task 7's tests and this probe); no PROTOCOL tokens (memory `feedback_protocol_token_discipline`). Record scenario × runs in RFC §7.

- [ ] **Step 3: Commit** — `git add scripts/validation/drive_routing_probe.py docs/10_rfcs/USER_DRIVE_RFC.md src/infrastructure/agent_manifest.py && git commit -m "test(drive): routing probe — single and two-turn"`

---

## Task 12: User-facing help + documentation

**Files:** `src/utils/capabilities.py`, `CLAUDE.md`, `src/agents/CLAUDE.md`, `docs/05_building_blocks/file_storage/README.md`, `docs/07_deployment/README.md`, `docs/10_rfcs/USER_DRIVE_RFC.md` (status line). Also, outside the repo: the "File resolution double-fetch" line in the memory index `MEMORY.md` is closed by Task 7 — remove it.

- [ ] **Step 1: `capabilities.py`** — after *File attachments*:

```
*Your drive (long-term files)*
Connect your drive in the Cabinet. Then:
- "Remember this file" — I keep it in your drive's Inbox, or in the folder you name, under a clear name
- "What's in Meetings?" / "Find the lease contract" — browse your folders and search by name
- Open, rename, move, create folders; add a line to a note; replace a file with a new version
- Delete a file or a whole folder — I post a note in chat with what was deleted; it stays in your drive's recycle bin
Chat attachments are temporary; the drive is where files are kept.
```

and "Files are kept for 90 days." → "Chat attachments are kept for 90 days; to keep a file, ask me to remember it on your drive."

- [ ] **Step 2: Root `CLAUDE.md`** — after **File Storage Pipeline**:

```
**User Drive** (`docs/10_rfcs/USER_DRIVE_RFC.md`) — the user's long-term file area: OneDrive App Folder
(`Apps/Alek-bot`, scope `Files.ReadWrite.AppFolder`) behind the provider-neutral `UserDrivePort` (`OneDriveAdapter`;
token refresh + 5-minute in-memory cache shared with To Do in `adapters/microsoft_graph_auth.py` — a disconnect
reaches every instance within 5 minutes, To Do included). The model and history see
only `drive:<opaque id>` refs and `[Drive: …]` labels with decoded paths — no provider name. Eight drive intents on
`FILE_MANAGEMENT`, store in the name (`*_to_drive`/`*_in_drive`/`*_from_drive`); mutations refuse a ref from the
other store, reads are lenient; `FILE_MANAGEMENT.prefetch_file_ref=False` (the coordinator no longer pre-downloads
its `file_ref`). `UserDriveService`: case-insensitive folders, saves never overwrite (same bytes → existing, else
suffixed name, NFC-normalized comparison), append-only text edits, root protected. Mutations of one user run one at
a time under `asyncio.shield`; the agent waits 100 s (inside its 120 s timeout) and otherwise tells the model the
operation is still running — never a false "failed"; delete/replace post a `notify_raw` receipt. Vision only for
JPEG/PNG/GIF/WebP and PDF (HEIC refused). Refs survive turns via `history_context["drive_context"]`
(full_text only, never consolidated). `Trivial_Exclusions` keeps file operations out of memory. Step 2 (indexing +
proper search), step 3 (long audio/video) and full editing are not built.
```

Agent table FileManagement row: intents `open_file`, `delete_file`, 8 × `*_drive`.

- [ ] **Step 3: `src/agents/CLAUDE.md`** — FileManagement bullet: both stores, the eight intents, strict/lenient, `prefetch_file_ref=False`, per-user mutation lock + shield + 100 s wait / "still running" + receipts, `drive_context`, 120 s timeout, vision formats.

- [ ] **Step 4: file_storage + deployment READMEs** — what lives where; Azure permission; redirect URI `<service URL>/auth/connect-onedrive/callback` registered on `Alek-bot` and stored as secret `ONEDRIVE_REDIRECT_URI_DEV`.

- [ ] **Step 5:** `make check` → green.

- [ ] **Step 6: Commit** — `git add src/utils/capabilities.py CLAUDE.md src/agents/CLAUDE.md docs/ && git commit -m "docs(drive): user drive in CLAUDE.md, roster, file storage, deployment, help"`

---

## Task 13: Deploy and live acceptance (with the owner)

**Owner prerequisites:** secret `ONEDRIVE_REDIRECT_URI_DEV`; the same URI on `Alek-bot`; branch check (`git branch --show-current` = `feat/user-drive`, clean tree — `make deploy` ships the working tree).

- [ ] **Step 1:** `make deploy`; `make fetch-logs K=200`; grep `OneDriveAdapter initialized`.
- [ ] **Step 2:** Owner: Cabinet → Drive → Connect; card shows connected.
- [ ] **Step 3: Live script (owner, Slack):**
  1. Send a PDF, "запомни этот файл" → `Inbox/`, readable name, no receipt.
  2. Same PDF again, "запомни" → "already on the drive".
  3. A different PDF with the same name → saved under a suffixed name, stated.
  4. "запомни в Встречи/2026" → folder created.
  5. Move a file by hand in OneDrive; next turn "открой его" → opens by the old ref.
  6. "переименуй в lease.pdf" → renamed, no receipt.
  7. Create a `.md` in OneDrive; "добавь строку …" → appended, no receipt; OneDrive shows the line.
  8. Send a new PDF version, "обнови договор этим файлом" → receipt with sizes; version history has the previous one.
  9. Search; then "открой второй" on the next turn.
  10. "удали lease.pdf" → receipt; "удали папку Встречи" → receipt with count; restore the folder from the recycle bin.
  11. "удали весь диск" → refusal.
- [ ] **Step 4:** `make fetch-logs K=500`, grep `Drive` errors in the window; Logfire for `file_management_agent` spans.
- [ ] **Step 5:** Report per step; RFC status "Step 1 live (YYYY-MM-DD)"; open the PR — the owner merges.
