#!/usr/bin/env python3
"""
Phase 0 spike for docs/10_rfcs/USER_DRIVE_RFC.md — probes Graph behaviour in the
owner's OneDrive App Folder before any adapter code is written.

Interactive: device-code flow — prints a code the owner enters at the Microsoft
device-login page on any device, so it runs from a remote session with no browser
and no redirect URI. Keeps tokens in memory only, writes a JSON report to
scripts/memory/ (gitignored).

Prerequisites on the app registration: delegated Files.ReadWrite.AppFolder, and
Authentication → "Allow public client flows" enabled (device code is a public-client
grant: no client secret). The client id is read from MS_CLIENT_ID, falling back to
MICROSOFT_TODO_CLIENT_ID.

    MS_CLIENT_ID=<id> python scripts/onedrive/probe_appfolder.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import quote, unquote

import aiohttp
from dotenv import load_dotenv

load_dotenv()

_AUTH = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
_GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPE = "Files.ReadWrite.AppFolder offline_access"
_OUT = Path(__file__).resolve().parents[1] / "memory"


async def _device_code_token(s: aiohttp.ClientSession, client_id: str) -> Dict[str, Any]:
    async with s.post(f"{_AUTH}/devicecode", data={"client_id": client_id, "scope": _SCOPE}) as r:
        dc = await r.json()
        if r.status != 200:
            raise RuntimeError(f"device code request failed: {dc}")
    print(dc["message"], flush=True)
    interval = int(dc.get("interval", 5))
    deadline = asyncio.get_running_loop().time() + int(dc.get("expires_in", 900))
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(interval)
        async with s.post(f"{_AUTH}/token", data={
            "client_id": client_id, "device_code": dc["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }) as r:
            tok = await r.json()
        if r.status == 200:
            return tok
        err = tok.get("error")
        if err == "slow_down":
            interval += 5
        elif err != "authorization_pending":
            raise RuntimeError(f"device code token failed: {tok}")
    raise RuntimeError("device code expired before sign-in")


def _pp(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"raw": raw}
    path = (raw.get("parentReference") or {}).get("path")
    return {"name": raw.get("name"), "parent_path": path,
            "parent_path_unquoted": unquote(path) if path else None}


async def main() -> int:
    client_id = (os.environ.get("MS_CLIENT_ID") or os.environ["MICROSOFT_TODO_CLIENT_ID"]).strip()
    report: Dict[str, Any] = {"at": datetime.now().isoformat()}
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    async with aiohttp.ClientSession() as s:
        tok = await _device_code_token(s, client_id)
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
