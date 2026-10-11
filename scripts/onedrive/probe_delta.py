#!/usr/bin/env python3
"""
Spikes S0a–S0f for docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §18 — delta, cTag, restore,
nudge latency, facets and thumbnails in the owner's OneDrive App Folder.

Same sign-in as probe_appfolder.py (device code, Files.ReadWrite.AppFolder, tokens in
memory only). Phase A runs unattended. Phase B prints instructions for the owner
(edit a .docx in Word for the web, drop a phone photo, move a file out of and into the
App Folder) and polls until the marker file scripts/memory/s0_done exists. Phase C
drains delta, reads thumbnails and deletes the probe folder. The raw report (names,
ids — PII) goes to scripts/memory/ (gitignored).

    python -u scripts/onedrive/probe_delta.py
    python -u scripts/onedrive/probe_delta.py --resume scripts/memory/onedrive_s0_<stamp>.json
    python -u scripts/onedrive/probe_delta.py --moves scripts/memory/onedrive_s0_<stamp>.json
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import aiohttp
from dotenv import load_dotenv
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_appfolder import _AUTH, _GRAPH, _SCOPE, _device_code_token  # noqa: E402

load_dotenv()

_OUT = Path(__file__).resolve().parents[1] / "memory"
_DONE = _OUT / "s0_done"
_PHASE_B_CAP_S = 45 * 60

_DOCX_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'),
    "word/document.xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>S0b probe. Edit this document for about ten minutes.</w:t></w:r></w:p>'
        '</w:body></w:document>'),
}


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, xml in _DOCX_PARTS.items():
            z.writestr(name, xml)
    return buf.getvalue()


def _image(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (40, 120, 200)).save(buf, fmt)
    return buf.getvalue()


def _brief(it: Dict[str, Any]) -> Dict[str, Any]:
    """Every field the RFC's policy and change capture read, nothing else."""
    f = it.get("file") or {}
    return {
        "id": it.get("id"), "name": it.get("name"),
        "parent_id": (it.get("parentReference") or {}).get("id"),
        "parent_path": (it.get("parentReference") or {}).get("path"),
        "deleted": it.get("deleted"), "root": "root" in it,
        "cTag": it.get("cTag"), "eTag": it.get("eTag"), "size": it.get("size"),
        "lastModifiedDateTime": it.get("lastModifiedDateTime"),
        "file": {"mimeType": f.get("mimeType"), "hashes": sorted((f.get("hashes") or {}).keys())} if f else None,
        "folder": it.get("folder"),
        "photo": it.get("photo"), "image": it.get("image"), "location": it.get("location"),
        "remoteItem": "remoteItem" in it, "package": it.get("package"),
        "facets": sorted(k for k in it if not k.startswith("@")),
    }


class Probe:
    def __init__(self, s: aiohttp.ClientSession, client_id: str, tok: Dict[str, Any]) -> None:
        self.s, self.client_id, self.tok = s, client_id, tok
        self.report: Dict[str, Any] = {"at": datetime.now().isoformat()}
        self.delta_link: Optional[str] = None

    async def _refresh(self) -> None:
        async with self.s.post(f"{_AUTH}/token", data={
            "client_id": self.client_id, "grant_type": "refresh_token",
            "refresh_token": self.tok["refresh_token"], "scope": _SCOPE,
        }) as r:
            tok = await r.json()
            if r.status != 200:
                raise RuntimeError(f"refresh failed: {tok}")
        self.tok = tok

    async def call(self, method: str, url: str, **kw: Any) -> Tuple[int, Any, Dict[str, str]]:
        extra = kw.pop("headers", {})
        for attempt in (0, 1):
            h = {"Authorization": f"Bearer {self.tok['access_token']}", **extra}
            async with self.s.request(method, url if url.startswith("http") else _GRAPH + url,
                                      headers=h, allow_redirects=False, **kw) as r:
                text = await r.text()
                if r.status == 401 and attempt == 0:
                    await self._refresh()
                    continue
                try:
                    body: Any = json.loads(text) if text else None
                except ValueError:
                    body = text[:500]
                return r.status, body, dict(r.headers)
        raise AssertionError("unreachable")

    async def drain(self, url: str) -> Dict[str, Any]:
        """Follow nextLink pages to a deltaLink; keeps the raw pages for wire fixtures."""
        pages: List[Dict[str, Any]] = []
        items: List[Dict[str, Any]] = []
        while True:
            st, body, hdr = await self.call("GET", url)
            if st != 200:
                return {"status": st, "body": body, "location": hdr.get("Location"), "pages": pages}
            pages.append(body)
            items.extend(body.get("value", []))
            nxt = body.get("@odata.nextLink")
            if nxt:
                url = nxt
                continue
            self.delta_link = body.get("@odata.deltaLink")
            return {"status": 200, "page_count": len(pages), "item_count": len(items),
                    "items": [_brief(i) for i in items], "raw_pages": pages}

    async def delta(self, label: str) -> Dict[str, Any]:
        assert self.delta_link
        res = await self.drain(self.delta_link)
        self.report.setdefault("delta_rounds", {})[label] = res
        print(f"[delta {label}] status={res['status']} items={res.get('item_count')}", flush=True)
        return res

    async def wait_seen(self, ids: Set[str], label: str, cap_s: int = 180) -> Dict[str, Any]:
        """Poll delta every 2 s until every id has appeared; seconds until each one did."""
        t0, seen, rounds = time.monotonic(), {}, []
        while ids - seen.keys() and time.monotonic() - t0 < cap_s:
            res = await self.drain(self.delta_link)  # type: ignore[arg-type]
            rounds.append(res)
            for it in res.get("items", []):
                if it["id"] in ids and it["id"] not in seen:
                    seen[it["id"]] = {"after_s": round(time.monotonic() - t0, 1), "cTag": it["cTag"],
                                      "eTag": it["eTag"]}
            if ids - seen.keys():
                await asyncio.sleep(2)
        out = {"seen": seen, "missing": sorted(ids - seen.keys()), "rounds": rounds}
        self.report[label] = out
        print(f"[{label}] seen={len(seen)} missing={len(out['missing'])}", flush=True)
        return out

    async def put(self, parent_id: str, name: str, data: bytes, ctype: str) -> Dict[str, Any]:
        st, body, _ = await self.call("PUT", f"/me/drive/items/{parent_id}:/{quote(name)}:/content",
                                      data=data, headers={"Content-Type": ctype})
        if st not in (200, 201):
            raise RuntimeError(f"PUT {name} → {st} {body}")
        return body

    async def thumbnails(self, item_id: str) -> Dict[str, Any]:
        st, body, _ = await self.call("GET", f"/me/drive/items/{item_id}/thumbnails")
        out: Dict[str, Any] = {"status": st}
        if st != 200:
            out["body"] = body
            return out
        sets = body.get("value", [])
        out["sets"] = []
        for ts in sets:
            sizes = {}
            for size in ("small", "medium", "large"):
                t = ts.get(size)
                if not t:
                    continue
                async with self.s.get(t["url"]) as r:
                    blob = await r.read()
                sizes[size] = {"w": t.get("width"), "h": t.get("height"), "fetch_status": r.status,
                               "content_type": r.headers.get("Content-Type"), "bytes": len(blob)}
            out["sets"].append(sizes)
        return out


async def run(p: Probe) -> None:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    r = p.report

    st, root, _ = await p.call("GET", "/me/drive/special/approot")
    root_id = root["id"]
    r["root"] = _brief(root)

    # --- S0a: initial enumeration, forced small pages to see pagination -----------------
    init = await p.drain("/me/drive/special/approot/delta?$top=3")
    if init["status"] == 400:  # $top refused — record it, then enumerate without it
        r["S0a_top_refused"] = init
        init = await p.drain("/me/drive/special/approot/delta")
    r["S0a_initial"] = init
    if init["status"] != 200:
        print(f"S0a FAILED: delta refused under AppFolder: {init['status']} {init['body']}", flush=True)
        return
    r["S0a_approot_in_delta"] = any(i["id"] == root_id for i in init["items"])
    print(f"[S0a initial] pages={init['page_count']} items={init['item_count']} "
          f"approot_present={r['S0a_approot_in_delta']}", flush=True)

    # --- fixture tree -------------------------------------------------------------------
    _, P, _ = await p.call("POST", f"/me/drive/items/{root_id}/children",
                           json={"name": f"S0 probe {stamp}", "folder": {},
                                 "@microsoft.graph.conflictBehavior": "fail"})
    _, S, _ = await p.call("POST", f"/me/drive/items/{P['id']}/children", json={"name": "sub", "folder": {}})
    one = await p.put(S["id"], "one.txt", b"one", "text/plain")
    two = await p.put(S["id"], "two.txt", b"two", "text/plain")
    three = await p.put(P["id"], "three.txt", b"three", "text/plain")
    move_out = await p.put(P["id"], "move-out.txt", b"move me out of the App Folder", "text/plain")
    jpg = await p.put(P["id"], "synthetic.jpg", _image("JPEG"), "image/jpeg")
    png = await p.put(P["id"], "synthetic.png", _image("PNG"), "image/png")
    doc = await p.put(P["id"], "edit-me.docx", _docx(),
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    created = {x["id"] for x in (P, S, one, two, three, move_out, jpg, png, doc)}
    r["fixture"] = {k: _brief(v) for k, v in
                    dict(P=P, S=S, one=one, two=two, three=three, move_out=move_out, jpg=jpg, png=png,
                         doc=doc).items()}
    await p.wait_seen(created, "S0a_creates_seen")

    # --- S0d: the bot's own upload, measured alone ----------------------------------------
    nudge = await p.put(P["id"], "nudge.txt", b"nudge", "text/plain")
    r["S0d_put_response"] = {"cTag": nudge.get("cTag"), "eTag": nudge.get("eTag")}
    await p.wait_seen({nudge["id"]}, "S0d_nudge_seen")

    # --- S0b part 1: cTag across rename + move; content edit for contrast ------------------
    _, before, _ = await p.call("GET", f"/me/drive/items/{one['id']}")
    _, after, _ = await p.call("PATCH", f"/me/drive/items/{one['id']}",
                               json={"parentReference": {"id": P["id"]}, "name": "one-renamed.txt"})
    edited = await p.put(P["id"], "nudge.txt", b"nudge, second version", "text/plain")
    r["S0b_rename_move"] = {
        "before": {"cTag": before["cTag"], "eTag": before["eTag"]},
        "after": {"cTag": after["cTag"], "eTag": after["eTag"], "id_kept": after["id"] == one["id"]},
        "cTag_stable": before["cTag"] == after["cTag"],
        "content_edit": {"before": nudge.get("cTag"), "after": edited.get("cTag")},
    }
    await p.delta("after_rename_move_edit")

    # --- S0a: pagination of an incremental round + non-empty folder delete ---------------
    await p.call("DELETE", f"/me/drive/items/{S['id']}")  # S still holds two.txt
    await asyncio.sleep(5)
    link = p.delta_link
    res = await p.drain(link + ("&" if "?" in link else "?") + "$top=1")  # type: ignore[operator]
    if res["status"] == 400:
        res = await p.drain(link)  # type: ignore[arg-type]
    r["S0a_folder_delete"] = {
        "page_count": res.get("page_count"),
        "tombstones": [i for i in res.get("items", []) if i["deleted"]],
        "folder_and_child": {"S": S["id"], "two": two["id"]},
        "all": res,
    }
    print(f"[S0a folder delete] tombstones={len(r['S0a_folder_delete']['tombstones'])} "
          f"pages={res.get('page_count')}", flush=True)

    # --- S0c: delete + restore via API ----------------------------------------------------
    await p.call("DELETE", f"/me/drive/items/{three['id']}")
    st, body, _ = await p.call("POST", f"/me/drive/items/{three['id']}/restore", json={})
    r["S0c_api_restore"] = {"status": st, "body": body if st >= 300 else _brief(body),
                            "id_kept": isinstance(body, dict) and body.get("id") == three["id"]}
    print(f"[S0c restore via API] status={st}", flush=True)
    await p.delta("after_restore")

    # --- S0f: synthetic images ------------------------------------------------------------
    r["S0f_synthetic"] = {"jpg": await p.thumbnails(jpg["id"]), "png": await p.thumbnails(png["id"])}

    # --- Phase B: owner actions ----------------------------------------------------------
    restore_by_hand = r["S0c_api_restore"]["status"] >= 300
    print("\n" + "=" * 72, flush=True)
    print(f"PHASE B — in OneDrive on the web, open the App Folder → 'S0 probe {stamp}':", flush=True)
    print("  1. Open edit-me.docx in Word for the web and type at a normal pace for ~10 min.", flush=True)
    print("  2. Upload a phone photo (HEIC if you have one, plus a JPEG with location) into this folder.", flush=True)
    print("  3. Move move-out.txt OUT of the App Folder (e.g. to the OneDrive root).", flush=True)
    print("  4. Move any small file from elsewhere in OneDrive INTO this folder.", flush=True)
    if restore_by_hand:
        print("  5. Restore three.txt from the OneDrive recycle bin.", flush=True)
    print("Then tell Claude 'done'.", flush=True)
    print("=" * 72 + "\n", flush=True)

    doc_log: List[Dict[str, Any]] = []
    last: Optional[Tuple[str, str]] = None
    t0, last_delta = time.monotonic(), time.monotonic()
    while not _DONE.exists() and time.monotonic() - t0 < _PHASE_B_CAP_S:
        st, d, _ = await p.call("GET", f"/me/drive/items/{doc['id']}")
        if st == 200:
            cur = (d.get("cTag"), d.get("eTag"))
            if cur != last:
                doc_log.append({"t_s": round(time.monotonic() - t0), "cTag": cur[0], "eTag": cur[1],
                                "lastModifiedDateTime": d.get("lastModifiedDateTime"), "size": d.get("size")})
                print(f"[S0b] t={doc_log[-1]['t_s']}s cTag/eTag changed (#{len(doc_log)})", flush=True)
                last = cur
        if time.monotonic() - last_delta > 60:
            await p.delta(f"phase_b_{round(time.monotonic() - t0)}s")
            last_delta = time.monotonic()
        await asyncio.sleep(15)
    _DONE.unlink(missing_ok=True)
    r["S0b_word_edit"] = {"duration_s": round(time.monotonic() - t0), "changes": doc_log}

    # --- Phase C ------------------------------------------------------------------------
    ids = {k: v["id"] for k, v in r["fixture"].items()}
    ids["nudge"] = nudge["id"]
    await phase_c(p, ids, restore_by_hand)


async def phase_c(p: Probe, ids: Dict[str, str], restore_by_hand: bool) -> None:
    r = p.report
    await p.delta("phase_c")
    if restore_by_hand:
        st, again, _ = await p.call("GET", f"/me/drive/items/{ids['P']}:/three.txt")
        r["S0c_manual_restore"] = {"status": st, "id_kept": isinstance(again, dict) and again.get("id") == ids["three"]}
    phase_bc = [(label, i) for label, rnd in r["delta_rounds"].items()
                if label.startswith(("phase_b", "phase_c")) for i in rnd.get("items", [])]
    r["S0e_move_out_in"] = {
        "move_out_id": ids["move_out"],
        "move_out_entries": [{"round": lb, **i} for lb, i in phase_bc if i["id"] == ids["move_out"]],
        "phase_bc_names": [{"round": lb, "name": i["name"], "deleted": i["deleted"]} for lb, i in phase_bc],
    }
    _, kids, _ = await p.call("GET", f"/me/drive/items/{ids['P']}/children")
    r["S0e_children_listing"] = [_brief(k) for k in kids.get("value", [])]
    r["S0f_owner_images"] = {
        k["name"]: await p.thumbnails(k["id"]) for k in kids.get("value", [])
        if k.get("image") or k.get("photo") or (k.get("file") or {}).get("mimeType", "").startswith("image/")
        or k.get("name", "").lower().endswith((".heic", ".heif"))
    }

    # Cleanup deletes only what this script created; the probe folder keeps the owner's files.
    own = ("one", "three", "move_out", "jpg", "png", "doc", "nudge")
    r["cleanup"] = {k: (await p.call("DELETE", f"/me/drive/items/{ids[k]}"))[0] for k in own}
    await asyncio.sleep(5)
    gone = await p.delta("cleanup")
    r["cleanup_tombstones"] = len([i for i in gone.get("items", []) if i["deleted"]])


async def resume(p: Probe, prior: Path) -> None:
    """Phase C only, from a report whose run was stopped in phase B."""
    old = json.loads(prior.read_text(encoding="utf-8"))
    p.report = {**old, "resumed_at": datetime.now().isoformat()}
    last_round = list(old["delta_rounds"].values())[-1]
    p.delta_link = last_round["raw_pages"][-1]["@odata.deltaLink"]
    ids = {k: v["id"] for k, v in old["fixture"].items()}
    ids["nudge"] = next(iter(old["S0d_nudge_seen"]["seen"]))
    await phase_c(p, ids, old["S0c_api_restore"]["status"] >= 300)


async def moves(p: Probe, prior: Path) -> None:
    """One delta round from a report's cursor after the owner moved files out of and into the App Folder,
    plus a GET of every item the previous round knew — a moved-out item that delta keeps silent about shows here."""
    old = json.loads(prior.read_text(encoding="utf-8"))
    fixture = old.get("fixture") or json.loads((prior.parent / old.get("prior_fixture_from", old["prior"])).read_text())["fixture"]
    p.report = {"at": datetime.now().isoformat(), "prior": prior.name,
                "prior_fixture_from": old.get("prior_fixture_from", prior.name), "fixture": fixture}
    last = list(old["delta_rounds"].values())[-1]
    p.delta_link = last["raw_pages"][-1]["@odata.deltaLink"]
    known = {i["id"]: i for i in last["items"] if not i["deleted"]}
    res = await p.delta("moves")
    for i in res.get("items", []):
        print(f"  {'DELETED' if i['deleted'] else 'live   '} {i['name']} parent={i['parent_path']}", flush=True)
    lookups = {}
    for item_id, before in known.items():
        st, body, _ = await p.call("GET", f"/me/drive/items/{item_id}")
        lookups[item_id] = {"name": before["name"], "status": st,
                            "parent_path": (body.get("parentReference") or {}).get("path") if st == 200 else None,
                            "body": body if st != 200 else None}
        if st != 200 or lookups[item_id]["parent_path"] != before["parent_path"]:
            print(f"  GET {before['name']}: {st} parent={lookups[item_id]['parent_path']}", flush=True)
    p.report["lookups"] = lookups
    _, kids, _ = await p.call("GET", f"/me/drive/items/{fixture['P']['id']}/children")
    p.report["probe_listing"] = [_brief(k) for k in kids.get("value", [])]
    seen = {i["id"] for i in res.get("items", [])} | set(known)
    for k in p.report["probe_listing"]:
        if k["id"] not in seen:
            print(f"  LISTED, never in delta: {k['name']}", flush=True)
    p.report["doc_delete"] = (await p.call("DELETE", f"/me/drive/items/{fixture['doc']['id']}"))[0]
    print(f"doc delete: {p.report['doc_delete']}", flush=True)


async def main() -> int:
    client_id = (os.environ.get("MS_CLIENT_ID") or os.environ["MICROSOFT_TODO_CLIENT_ID"]).strip()
    _OUT.mkdir(parents=True, exist_ok=True)
    _DONE.unlink(missing_ok=True)
    out = _OUT / f"onedrive_s0_{datetime.now():%Y%m%d%H%M%S}.json"
    async with aiohttp.ClientSession(trust_env=True) as s:  # honour HTTPS_PROXY where egress is proxied
        p = Probe(s, client_id, await _device_code_token(s, client_id))
        try:
            if len(sys.argv) > 2 and sys.argv[1] == "--resume":
                await resume(p, Path(sys.argv[2]))
            elif len(sys.argv) > 2 and sys.argv[1] == "--moves":
                await moves(p, Path(sys.argv[2]))
            else:
                await run(p)
        finally:
            out.write_text(json.dumps(p.report, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"\nReport → {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
