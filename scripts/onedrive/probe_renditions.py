#!/usr/bin/env python3
"""
Spike S0g for docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §18 / §24.5 — provider renditions as model input.
OWNER-RUN. Do Graph custom thumbnail sizes (`c{w}x{h}`) work for App Folder items under Files.ReadWrite.AppFolder,
up to which size, for which formats (HEIC included), and is receipt and A4 text legible in them?

    python -u scripts/onedrive/probe_renditions.py "<folder path inside the App Folder>"

Put a few test files in that folder first: a phone HEIC of a receipt, a JPEG and a PNG screenshot of an A4 page, a
scanned PDF, a photo. Same sign-in as probe_appfolder.py (device code, tokens in memory only). For every file
directly in the folder and every size in SIZES the script asks `GET /items/{id}/thumbnails?select=<size>` (the call
OneDriveAdapter.get_thumbnail makes), fetches the rendition URL without the bearer token, and saves the bytes as
scripts/memory/s0g_<stamp>/<n>_<size>.<ext> — the input of `drive_model_check triage|read` — next to report.json
(names, status, width x height, bytes, content type per size). Everything it writes is PII and stays in scripts/memory
(gitignored). Then the owner opens the renditions and notes the smallest size at which receipt and A4 text is legible.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_appfolder import _device_code_token  # noqa: E402
from probe_delta import Probe  # noqa: E402

load_dotenv()

_OUT = Path(__file__).resolve().parents[1] / "memory"
SIZES = ("medium", "large", "c1024x1024", "c2048x2048", "c4096x4096")
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}


def files_of(children: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The files directly in the folder (folders skipped), in name order."""
    return sorted((c for c in children if "file" in c), key=lambda c: str(c.get("name", "")).lower())


def rendition_url(page: Any, size: str) -> Optional[str]:
    """The rendition URL in a `/thumbnails?select=<size>` page, as OneDriveAdapter.get_thumbnail reads it."""
    sets = page.get("value") if isinstance(page, dict) else None
    first = sets[0] if isinstance(sets, list) and sets and isinstance(sets[0], dict) else {}
    rendition = first.get(size)
    url = rendition.get("url") if isinstance(rendition, dict) else None
    return url if isinstance(url, str) and url.startswith("https://") else None


def summarize(report: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """Per size: how many files got a rendition, by the file's mime type."""
    out: Dict[str, Dict[str, int]] = {size: {} for size in SIZES}
    for item in report.get("items", []):
        for size, row in item.get("sizes", {}).items():
            if row.get("saved"):
                out[size][item["mime"]] = out[size].get(item["mime"], 0) + 1
    return out


async def probe(p: Probe, plain: aiohttp.ClientSession, folder: str, out_dir: Path) -> Dict[str, Any]:
    st, root, _ = await p.call("GET", f"/me/drive/special/approot:/{quote(folder.strip('/'))}")
    if st != 200:
        raise RuntimeError(f"folder not found in the App Folder: {st}")
    _, kids, _ = await p.call("GET", f"/me/drive/items/{root['id']}/children?$top=200")
    report: Dict[str, Any] = {"at": datetime.now().isoformat(), "folder": folder, "sizes": SIZES, "items": []}
    for n, item in enumerate(files_of(kids.get("value", [])), start=1):
        row: Dict[str, Any] = {"n": n, "name": item.get("name"), "mime": (item.get("file") or {}).get("mimeType", ""),
                               "bytes": item.get("size"), "sizes": {}}
        for size in SIZES:
            st, page, _ = await p.call("GET", f"/me/drive/items/{item['id']}/thumbnails?select={size}")
            url = rendition_url(page, size) if st == 200 else None
            entry: Dict[str, Any] = {"status": st, "saved": False}
            if url:
                rendition = ((page.get("value") or [{}])[0]).get(size) or {}
                entry.update(width=rendition.get("width"), height=rendition.get("height"))
                async with plain.get(url) as r:
                    data = await r.read()
                    ctype = r.headers.get("Content-Type", "").split(";")[0]
                entry.update(fetch=r.status, content_type=ctype, rendition_bytes=len(data))
                if r.status == 200 and data:
                    (out_dir / f"{n:02d}_{size}.{_EXT.get(ctype, 'bin')}").write_bytes(data)
                    entry["saved"] = True
            elif st != 200:
                entry["body"] = page
            row["sizes"][size] = entry
            print(f"  {n:02d} {row['mime']:<28} {size:<11} status={st} saved={entry['saved']} "
                  f"{entry.get('width')}x{entry.get('height')} {entry.get('rendition_bytes', 0)} B", flush=True)
        report["items"].append(row)
    report["summary"] = summarize(report)
    return report


async def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    client_id = (os.environ.get("MS_CLIENT_ID") or os.environ["MICROSOFT_TODO_CLIENT_ID"]).strip()
    out_dir = _OUT / f"s0g_{datetime.now():%Y%m%d%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession(trust_env=True) as s, aiohttp.ClientSession(trust_env=True) as plain:
        p = Probe(s, client_id, await _device_code_token(s, client_id))
        report = await probe(p, plain, sys.argv[1], out_dir)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nRenditions and report → {out_dir}", flush=True)
    print(json.dumps(report["summary"], indent=1), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
