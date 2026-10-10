#!/usr/bin/env python3
"""
A6 re-probe for docs/10_rfcs/USER_DRIVE_RFC.md §7 — does search work inside the App Folder
when indexing is given minutes, not 60 seconds?

Creates one text file in a nested folder of the App Folder, then polls two search forms
every 30 s for up to --wait-min minutes:
  /me/drive/special/approot/search(q=…)       (what the adapter uses)
  /me/drive/items/{approot-id}/search(q=…)    (same scope, addressed by id)
for a name query and a content query. Deletes its folder at the end. Device-code sign-in,
tokens in memory only, report to scripts/memory/ (gitignored).

    MS_CLIENT_ID=<id> python scripts/onedrive/probe_search.py --wait-min 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import aiohttp
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_appfolder import _GRAPH, _OUT, _device_code_token, _pp  # noqa: E402

load_dotenv()


async def main(wait_min: int) -> int:
    client_id = (os.environ.get("MS_CLIENT_ID") or os.environ["MICROSOFT_TODO_CLIENT_ID"]).strip()
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    report: Dict[str, Any] = {"at": datetime.now().isoformat(), "wait_min": wait_min}
    async with aiohttp.ClientSession(trust_env=True) as s:  # honour HTTPS_PROXY where egress is proxied
        tok = await _device_code_token(s, client_id)
        h = {"Authorization": f"Bearer {tok['access_token']}"}

        async def call(method: str, url: str, **kw: Any) -> Tuple[int, Any]:
            async with s.request(method, _GRAPH + url, headers={**h, **kw.pop("headers", {})}, **kw) as r:
                text = await r.text()
                try:
                    return r.status, (json.loads(text) if text else None)
                except ValueError:
                    return r.status, text

        st, root = await call("GET", "/me/drive/special/approot")
        root_id = root["id"]
        st, folder = await call("POST", f"/me/drive/items/{root_id}/children",
                                json={"name": f"Поиск {stamp}", "folder": {},
                                      "@microsoft.graph.conflictBehavior": "fail"})
        folder_id = folder["id"]
        name = f"протокол встречи {stamp}.txt"
        st, f = await call("PUT", f"/me/drive/items/{folder_id}:/{quote(name)}:/content",
                           data=f"searchuniq{stamp} договор аренды".encode(),
                           headers={"Content-Type": "text/plain"})
        report["upload_status"] = st

        queries = {"name": f"протокол встречи {stamp}", "name_token": f"{stamp}",
                   "content": f"searchuniq{stamp}"}
        forms = {"special": "/me/drive/special/approot/search(q='{q}')",
                 "by_id": f"/me/drive/items/{root_id}/search(q='{{q}}')"}
        found: Dict[str, Any] = {}
        t0 = time.monotonic()
        while time.monotonic() - t0 < wait_min * 60 and len(found) < len(queries) * len(forms):
            for qk, q in queries.items():
                for fk, tmpl in forms.items():
                    key = f"{fk}:{qk}"
                    if key in found:
                        continue
                    st, res = await call("GET", tmpl.format(q=quote(q.replace("'", "''"))))
                    hits: List[Any] = res.get("value", []) if isinstance(res, dict) else []
                    if st != 200:
                        found.setdefault(f"{key}:error", {"status": st, "body": str(res)[:300]})
                    if hits:
                        found[key] = {"after_s": int(time.monotonic() - t0), "status": st,
                                      "hits": [_pp(x) for x in hits]}
                        print(f"hit {key} after {found[key]['after_s']} s", flush=True)
            await asyncio.sleep(30)
        report["found"] = found
        report["missing"] = [f"{fk}:{qk}" for qk in queries for fk in forms if f"{fk}:{qk}" not in found]
        st, _ = await call("DELETE", f"/me/drive/items/{folder_id}")
        report["cleanup_delete_status"] = st

    _OUT.mkdir(parents=True, exist_ok=True)
    out = _OUT / f"onedrive_search_probe_{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\nReport → {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-min", type=int, default=10)
    sys.exit(asyncio.run(main(ap.parse_args().wait_min)))
