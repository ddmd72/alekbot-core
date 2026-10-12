#!/usr/bin/env python3
"""
Build PII-free wire fixtures for the OneDrive delta adapter from the spike S0a reports
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §17.3, §18).

The raw reports `scripts/memory/onedrive_s0_*.json` (written by probe_delta.py) hold the owner's
file names, ids, paths, URLs, hashes, account names and photo locations, so they stay gitignored.
This script copies the Graph responses the adapter tests need and replaces every identifying value
consistently (one map for the whole run: an id is the same fake id in every fixture), keeping the
structure Graph sent. It is default-deny: a key outside `_ALLOWED_KEYS` is dropped, never copied.
It then checks that no original identifying value survives in the output and fails loudly if one
does.

Each scenario reads one named report (`_SOURCES`), so a later probe run in scripts/memory/ never
changes a fixture silently; every fixture records its source report in `_meta.source`.

    python scripts/onedrive/sanitize_delta_fixtures.py --source <dir with onedrive_s0_*.json>
    python scripts/onedrive/sanitize_delta_fixtures.py --source <dir> --pin facet_heic=onedrive_s0_<stamp>.json

Only the output (tests/fixtures/onedrive_delta/*.json) is committed. The 410 fixture is synthetic:
S0a never saw a 410, so it follows the documented shape (410 Gone, a resyncChanges… error code and
a Location header).
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set
from urllib.parse import unquote

_REPO = Path(__file__).resolve().parents[2]
_FAKE_DRIVE = "ABCDEF0000000001"
_GRAPH_DELTA = "https://graph.microsoft.com/v1.0/me/drive/special/approot/delta"
_APP_NAME = "Alek-bot"
_GUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_TOKEN = re.compile(r"token=[^&]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# The report each scenario is built from (the S0a run of 2026-10-11). Override with --pin.
_SOURCES: Dict[str, str] = {
    "initial": "onedrive_s0_20261011222430.json",          # S0a_initial (approot taken from it)
    "folder_delete": "onedrive_s0_20261011222430.json",    # S0a_folder_delete
    "delete_restore": "onedrive_s0_20261011222430.json",   # delta_rounds.after_restore
    "moves": "onedrive_s0_20261011223343.json",            # --moves run with lookups (move out / in)
    "reemitted": "onedrive_s0_20261011223015.json",        # --moves run without lookups (41 entries)
    "facet_heic": "onedrive_s0_20261011222430.json",
    "facet_jpeg": "onedrive_s0_20261011223015.json",
    "facet_video": "onedrive_s0_20261011223015.json",
}
# Reports whose items count as "seen before" when the move-in (a first sighting) is picked.
_MOVES_BASELINE = ("onedrive_s0_20261011222430.json", "onedrive_s0_20261011223015.json")

# Default-deny: every key the fixtures keep. Anything else is dropped and reported.
_ALLOWED_KEYS = {
    "@odata.context", "@odata.deltaLink", "@odata.nextLink", "value",
    "id", "name", "size", "cTag", "eTag", "webUrl", "createdDateTime", "lastModifiedDateTime",
    "isAuthoritative", "deleted", "state", "createdBy", "lastModifiedBy", "application", "user",
    "displayName", "email", "shared", "owner", "scope", "parentReference", "driveId", "driveType",
    "path", "siteId", "fileSystemInfo", "folder", "childCount", "view", "specialFolder", "file",
    "mimeType", "hashes", "quickXorHash", "sha1Hash", "sha256Hash", "crc32Hash", "image", "width",
    "height", "photo", "takenDateTime", "cameraMake", "cameraModel", "exposureDenominator",
    "exposureNumerator", "fNumber", "focalLength", "iso", "orientation", "location", "latitude",
    "longitude", "altitude", "video", "audioBitsPerSample", "audioChannels", "audioSamplesPerSecond",
    "bitrate", "duration", "fourCC", "frameRate", "error", "code", "message", "innerError", "date",
    "request-id", "client-request-id",
}
# Keys whose string values identify the owner or the owner's files.
_ID_KEYS = {"id", "driveId", "siteId"}
_HASH_KEYS = {"quickXorHash", "sha1Hash", "sha256Hash", "crc32Hash"}
_SYNTHETIC = {"takenDateTime": "2025-08-14T18:22:05Z", "cameraMake": "Apple", "cameraModel": "iPhone 15",
              "latitude": 41.9028, "longitude": 12.4964, "altitude": 21.0}


class Sanitizer:
    """One consistent replacement map for the whole run; remembers every original it replaced."""

    def __init__(self, real_drive_id: str, approot_name: str) -> None:
        self.real_drive = real_drive_id.upper()
        self.maps: Dict[str, Dict[str, str]] = {}
        self.originals: Set[str] = set()
        self.name_originals: Set[str] = set()     # names and path segments: checked without a length floor
        self.names: Dict[str, str] = {approot_name: _APP_NAME}
        self.dropped: Set[str] = set()

    def _mapped(self, kind: str, original: str, make: Callable[[int], str]) -> str:
        table = self.maps.setdefault(kind, {})
        if original not in table:
            table[original] = make(len(table) + 1)
        self.originals.add(original)
        return table[original]

    def ident(self, value: str) -> str:
        if value.upper() == self.real_drive:
            self.originals.add(value)
            return _FAKE_DRIVE
        if "!" in value:
            return self._mapped("item", value, lambda n: f"{_FAKE_DRIVE}!i{n:04d}")
        if _GUID.fullmatch(value):
            return self.guid(value)
        return self._mapped("other", value, lambda n: f"X{n:04d}")

    def guid(self, value: str) -> str:
        return self._mapped("guid", value.lower(), lambda n: f"aaaaaaaa-0000-4000-a000-{n:012d}")

    def name(self, value: str, folder: bool) -> str:
        if value in self.names:
            return self.names[value]
        self.originals.add(value)
        self.name_originals.add(value)
        n = len(self.names)
        if folder:
            new = f"folder-{n:04d}"
        else:
            ext = value.rsplit(".", 1)[1] if "." in value.strip(".") else ""
            new = f"file-{n:04d}.{ext}" if ext else f"file-{n:04d}"
        self.names[value] = new
        return new

    def path(self, value: str) -> str:
        """'/drive/root:/A/B' → '/drive/root:/<mapped A>/<mapped B>' (segments are folder names)."""
        _, sep, rest = unquote(value).partition("root:")
        if not sep:
            return self._mapped("path", value, lambda n: f"/drive/root:/unknown-{n:04d}")
        self.originals.add(value)
        segments = [s for s in rest.split("/") if s]
        return "/drive/root:" + "".join(f"/{self.name(s, folder=True)}" for s in segments)

    def url(self, value: str) -> str:
        return self._mapped("url", value, lambda n: f"https://onedrive.example.invalid/item/{n:04d}")

    def link(self, value: str) -> str:
        """nextLink / deltaLink: keep the Graph path and $top, replace the opaque token."""
        def swap(m: "re.Match[str]") -> str:
            return "token=" + self._mapped("token", m.group(0), lambda n: f"tok{n:04d}")
        return _TOKEN.sub(swap, value)

    def digest(self, value: str) -> str:
        return self._mapped("hash", value, lambda n: base64.b64encode(f"fake-hash-{n:08d}-".encode()[:20]).decode())

    def etag(self, value: str) -> str:
        """'"c:{GUID},3"' keeps its shape; the GUID (derived from the item id) is replaced."""
        return _GUID.sub(lambda m: self.guid(m.group(0)).upper(), value)

    def clean(self, value: Any, key: str = "", parent: str = "") -> Any:
        if isinstance(value, dict):
            folder = "folder" in value or "specialFolder" in value
            out: Dict[str, Any] = {}
            for k, v in value.items():
                if k not in _ALLOWED_KEYS:
                    self.dropped.add(k)
                elif k == "name" and isinstance(v, str) and key != "specialFolder":
                    out[k] = self.name(v, folder)
                elif k in _SYNTHETIC and key in ("photo", "location"):
                    if v != _SYNTHETIC[k]:
                        self.originals.add(str(v))
                    out[k] = _SYNTHETIC[k]
                else:
                    out[k] = self.clean(v, k, key)
            return out
        if isinstance(value, list):
            return [self.clean(v, key, parent) for v in value]
        if not isinstance(value, str):
            return value
        if key in _ID_KEYS:
            return self.ident(value)
        if key == "path":
            return self.path(value)
        if key == "webUrl":
            return self.url(value)
        if key in ("@odata.nextLink", "@odata.deltaLink"):
            return self.link(value)
        if key in _HASH_KEYS:
            return self.digest(value)
        if key in ("cTag", "eTag"):
            return self.etag(value)
        if key == "displayName":
            self.originals.add(value)
            return _APP_NAME if parent == "application" else "Owner"
        if key == "email":
            self.originals.add(value)
            return "redacted"
        return _GUID.sub(lambda m: self.guid(m.group(0)), value)


# -- report access ------------------------------------------------------------------------------

def _report(source: Path, name: str) -> Dict[str, Any]:
    path = source / name
    if not path.exists():
        raise SystemExit(f"Pinned report {name} is not in {source}; pass --pin to choose another")
    data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _stamp(name: str) -> str:
    """'onedrive_s0_20261011222430.json' → '20261011222430' (a timestamp, not PII)."""
    return name.removeprefix("onedrive_s0_").removesuffix(".json")


def _items(pages: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for page in pages:
        yield from page.get("value", [])


def _seen_ids(report: Dict[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for rnd in report.get("delta_rounds", {}).values():
        out.update(i["id"] for i in _items(rnd.get("raw_pages", [])))
    out.update(i["id"] for i in _items(report.get("S0a_initial", {}).get("raw_pages", [])))
    return out


def _first_item(report: Dict[str, Any], test: Callable[[Dict[str, Any]], bool]) -> Dict[str, Any]:
    for rnd in report.get("delta_rounds", {}).values():
        for item in _items(rnd.get("raw_pages", [])):
            if test(item):
                return item
    raise SystemExit("No item of the requested shape in the pinned report")


# -- scenarios ----------------------------------------------------------------------------------

def build(source: Path, sources: Dict[str, str]) -> Dict[str, Any]:
    r = {scenario: _report(source, name) for scenario, name in sources.items()}
    initial = r["initial"]["S0a_initial"]["raw_pages"]
    approot = next(i for i in _items(initial) if "specialFolder" in i)
    san = Sanitizer(approot["parentReference"]["driveId"], approot["name"])

    folder_delete = r["folder_delete"]["S0a_folder_delete"]
    restore_pages = r["delete_restore"]["delta_rounds"]["after_restore"]["raw_pages"]
    restore_id = r["delete_restore"]["fixture"]["three"]["id"]
    moves = r["moves"]
    reemitted = r["reemitted"]["delta_rounds"]["moves"]["raw_pages"]

    moves_pages = moves["delta_rounds"]["moves"]["raw_pages"]
    gone = {k: v for k, v in moves["lookups"].items() if v["status"] == 404}
    moved_out = next(i["id"] for i in _items(moves_pages) if "deleted" in i and i["id"] in gone)
    earlier: Set[str] = set()
    for name in _MOVES_BASELINE:
        earlier |= _seen_ids(_report(source, name))
    moved_in = next(i["id"] for i in _items(moves_pages) if "deleted" not in i and i["id"] not in earlier)

    heic = _first_item(r["facet_heic"], lambda i: "location" in i and bool((i.get("photo") or {}).get("takenDateTime")))
    jpeg = _first_item(r["facet_jpeg"], lambda i: (i.get("file") or {}).get("mimeType") == "image/jpeg"
                       and i.get("photo") == {})
    video = _first_item(r["facet_video"], lambda i: "video" in i)

    def meta(scenario: str, text: str, **extra: Any) -> Dict[str, Any]:
        return {"scenario": text, "source": _stamp(sources[scenario]), **extra}

    out: Dict[str, Any] = {}
    # Order fixes the numbering of fake ids and names, so a re-run gives identical files.
    out["approot.json"] = san.clean(approot)
    out["initial_enumeration.json"] = {
        "_meta": meta("initial", "onboarding: $top=3 pages, nextLink twice then deltaLink; approot on every page"),
        "pages": san.clean(initial),
    }
    out["folder_delete.json"] = {
        "_meta": meta("folder_delete", "a non-empty folder deleted: a tombstone per descendant (OneDrive personal)",
                      folder_id=san.ident(folder_delete["folder_and_child"]["S"]),
                      child_id=san.ident(folder_delete["folder_and_child"]["two"])),
        "pages": san.clean(folder_delete["all"]["raw_pages"]),
    }
    out["delete_restore.json"] = {
        "_meta": meta("delete_restore", "a delete and a recycle-bin restore of the same item in one round",
                      item_id=san.ident(restore_id)),
        "pages": san.clean(restore_pages),
    }
    out["move_out_in.json"] = {
        "_meta": meta("moves", "a move out of the App Folder (tombstone, then GET 404) and a move in (first sighting)",
                      moved_out_id=san.ident(moved_out), moved_in_id=san.ident(moved_in)),
        "pages": san.clean(moves_pages),
        "get_moved_out": {"status": 404, "body": san.clean(gone[moved_out]["body"])},
    }
    out["reemitted.json"] = {
        "_meta": meta("reemitted", "a round with two real moves: unchanged items re-emitted, up to three times"),
        "pages": san.clean(reemitted),
    }
    out["facets.json"] = {
        "_meta": {"scenario": "S0e facet shapes: phone HEIC with photo+location, messenger JPEG with photo {}, video",
                  "source": {k: _stamp(sources[k]) for k in ("facet_heic", "facet_jpeg", "facet_video")}},
        "heic_photo_location": san.clean(heic),
        "jpeg_empty_photo": san.clean(jpeg),
        "video": san.clean(video),
    }
    out["resync_410.json"] = {
        "_meta": {"scenario": "SYNTHETIC: never observed in S0a; the documented 410 shape"},
        "status": 410,
        "headers": {"Location": f"{_GRAPH_DELTA}?token=tok-resync"},
        "body": {"error": {"code": "resyncChangesApplyDifferences",
                           "message": "Resync required. Replace any local items with the server's version."}},
    }
    _check(out, san)
    if san.dropped:
        print(f"Dropped keys outside the allowlist: {sorted(san.dropped)}")
    return out


def _strings(value: Any, key: str = "") -> Iterable[tuple]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, key)
    elif isinstance(value, str):
        yield key, value


def _check(out: Dict[str, Any], san: Sanitizer) -> None:
    text = json.dumps(out, ensure_ascii=False)
    leaks = sorted(o for o in san.originals if len(o) >= 4 and o != _APP_NAME and o in text)
    names_out: Set[str] = set()
    for key, value in _strings(out):
        if key == "name":
            names_out.add(value)
        elif key == "path":
            names_out.update(s for s in value.split("/") if s)
    leaks += sorted((san.name_originals - {_APP_NAME}) & names_out)   # exact match, no length floor
    if leaks:
        raise SystemExit(f"{len(leaks)} original values survived sanitising; fix the sanitiser")
    if "!s" in text or _EMAIL.search(text) or san.real_drive.lower() in text.lower():
        raise SystemExit("An item id, an email address or the drive id survived sanitising")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=_REPO / "scripts" / "memory")
    parser.add_argument("--out", type=Path, default=_REPO / "tests" / "fixtures" / "onedrive_delta")
    parser.add_argument("--pin", action="append", default=[], metavar="SCENARIO=REPORT",
                        help=f"use another report for a scenario; scenarios: {', '.join(_SOURCES)}")
    args = parser.parse_args(argv)
    sources = dict(_SOURCES)
    for pin in args.pin:
        scenario, _, name = pin.partition("=")
        if scenario not in sources or not name:
            parser.error(f"bad --pin {pin!r}")
        sources[scenario] = name
    files = build(args.source, sources)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (args.out / name).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {len(files)} sanitised fixtures to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
