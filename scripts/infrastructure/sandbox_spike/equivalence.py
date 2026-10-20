"""Spike S0 item g: do the two engines read the same bytes the same way? (UNTRUSTED_CONTENT_SANDBOX_RFC §7 S0)

    equivalence.py --dump DIR [--chat-dir DIR]     one JSON line per file on stdout, no text:
                                                   {file_id, kind, status, text_len, text_sha256}
    equivalence.py --compare A.jsonl B.jsonl       differences between two dumps (status and text hash per file_id)
    equivalence.py --show FILE_ID --dir DIR [--against TEXT_FILE] [--window N]
                                                   a bounded window of the LOCAL text for one file; with --against,
                                                   the window around the first difference from that text. Local only,
                                                   run on a mismatch; deployed text is never printed.

`--dump` runs every file of DIR through `SubprocessContentExtractor` (the drive parser, kind derived as the tick does:
kind_of + sniff_kind) and every chat-convertible file through `convert_file_to_text`. Run it locally and as the
Cloud Run Job in the repo image; the log lines of the other loggers in between are ignored by `--compare`.
Output goes through `print` (standalone script).
"""
import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

# Extensions the chat path converts (markitdown or plain text); PDFs and images go to the model natively there.
CHAT_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".csv", ".txt", ".md", ".html", ".json", ".rtf", ".eml", ".xml"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _files(root: Path) -> List[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _kind_for(path: Path) -> str:
    from src.domain.drive_index_policy import kind_of
    from src.domain.drive_sniff import SNIFF_HEAD_BYTES, sniff_kind

    with open(path, "rb") as fh:
        head = fh.read(SNIFF_HEAD_BYTES)
    return sniff_kind(head, kind_of(path.name, "", False))


async def _parser_text(extractor: Any, path: Path, kind: str) -> Dict[str, Any]:
    from src.domain.drive_extraction import ExtractionRequest

    outcome = await extractor.extract(ExtractionRequest(path=str(path), name=path.name, kind=kind, mime_type="",
                                                        user_id="s0", account_id="s0"))
    return {"status": outcome.status.value, "text": outcome.text}


async def _chat_text(path: Path) -> Dict[str, Any]:
    from src.utils.file_conversion import convert_file_to_text

    mime = mimetypes.guess_type(path.name)[0] or ""
    text = await convert_file_to_text(str(path), path.name, mime, None)
    return {"status": "alert" if text.startswith("[System:") else "text", "text": text}


async def _dump(root: Path, chat_root: Optional[Path]) -> int:
    from src.adapters.subprocess_content_extractor import PARSER_USER_ENV, SubprocessContentExtractor

    extractor = SubprocessContentExtractor(sandbox_user=os.getenv(PARSER_USER_ENV) or None)
    for path in _files(root):
        rel = str(path.relative_to(root))
        kind = _kind_for(path)
        try:
            got = await _parser_text(extractor, path, kind)
        except Exception as exc:        # a crash is a row, not the end of the dump
            got = {"status": f"exception:{type(exc).__name__}", "text": ""}
        print(json.dumps({"file_id": rel, "kind": kind, "status": got["status"], "text_len": len(got["text"]),
                          "text_sha256": _sha(got["text"])}), flush=True)
    for path in _files(chat_root or root):
        if path.suffix.lower() not in CHAT_EXTENSIONS:
            continue
        rel = "chat:" + str(path.relative_to(chat_root or root))
        try:
            got = await _chat_text(path)
        except Exception as exc:
            got = {"status": f"exception:{type(exc).__name__}", "text": ""}
        print(json.dumps({"file_id": rel, "kind": "chat", "status": got["status"], "text_len": len(got["text"]),
                          "text_sha256": _sha(got["text"])}), flush=True)
    return 0


def _load(path: str) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue            # log lines of the other loggers
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "file_id" in row and "text_sha256" in row:
                rows[row["file_id"]] = row
    return rows


def _compare(a_path: str, b_path: str) -> int:
    a, b = _load(a_path), _load(b_path)
    differences: List[Dict[str, Any]] = []
    for file_id in sorted(set(a) | set(b)):
        left, right = a.get(file_id), b.get(file_id)
        if left is None or right is None:
            differences.append({"file_id": file_id, "difference": "only in " + ("B" if left is None else "A")})
            continue
        changed = [k for k in ("status", "text_len", "text_sha256") if left.get(k) != right.get(k)]
        if changed:
            differences.append({"file_id": file_id, "differs": changed,
                                "A": {k: left.get(k) for k in changed}, "B": {k: right.get(k) for k in changed}})
    print(json.dumps({"files_a": len(a), "files_b": len(b), "equal": len(set(a) & set(b)) - sum(
        1 for d in differences if "differs" in d), "differences": differences}, indent=2))
    return 1 if differences else 0


async def _show(file_id: str, root: Path, against: Optional[str], window: int) -> int:
    chat = file_id.startswith("chat:")
    path = root / file_id.removeprefix("chat:")
    if chat:
        got = await _chat_text(path)
    else:
        from src.adapters.subprocess_content_extractor import PARSER_USER_ENV, SubprocessContentExtractor

        got = await _parser_text(SubprocessContentExtractor(sandbox_user=os.getenv(PARSER_USER_ENV) or None),
                                 path, _kind_for(path))
    text = got["text"]
    if against is None:
        print(f"status={got['status']} length={len(text)}\n--- first {window} chars ---\n{text[:window]}")
        return 0
    with open(against, encoding="utf-8", errors="replace") as fh:
        other = fh.read()
    index = next((i for i, (x, y) in enumerate(zip(text, other)) if x != y), min(len(text), len(other)))
    start = max(0, index - window // 2)
    print(f"first difference at char {index} (lengths {len(text)} vs {len(other)})")
    print(f"--- local ---\n{text[start:start + window]!r}\n--- against ---\n{other[start:start + window]!r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dump", metavar="DIR")
    parser.add_argument("--chat-dir", metavar="DIR", help="chat fixtures (default: the --dump directory)")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"))
    parser.add_argument("--show", metavar="FILE_ID")
    parser.add_argument("--dir", metavar="DIR", help="the corpus directory for --show")
    parser.add_argument("--against", metavar="TEXT_FILE")
    parser.add_argument("--window", type=int, default=400)
    args = parser.parse_args()
    if args.dump:
        return asyncio.run(_dump(Path(args.dump), Path(args.chat_dir) if args.chat_dir else None))
    if args.compare:
        return _compare(*args.compare)
    if args.show and args.dir:
        return asyncio.run(_show(args.show, Path(args.dir), args.against, args.window))
    parser.error("one of --dump DIR, --compare A B, --show FILE_ID --dir DIR is required")
    return 2


if __name__ == "__main__":
    sys.exit(main())
