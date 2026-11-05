"""Synthetic OLE2 (compound file, CFB v3) containers for the D13 macro check of legacy Office files (EXTERNAL_FILES
owner decision 2026-11-05, `parse.container_facts`). `olefile` can read but not write, so `build_ole` writes the minimal
format by hand: a header, FAT sectors, a directory (the children of a storage as a balanced binary tree of left/right siblings), and stream data
in regular 512-byte sectors (every stream is padded to the 4096-byte mini-stream cutoff, so no mini stream is needed).
Nothing here is a real document; the content is invented (`invoice_bathroom_es`, Reformas Exemplo Lda.).

    build_ole(tree)            tree = {name: bytes (a stream) | dict (a storage)} -> the bytes of the compound file
    word_with_macros()         a .doc: `WordDocument` stream + `Macros/VBA/ThisDocument` (a VBA project)
    excel_with_macros()        a .xls: `Workbook` stream + `_VBA_PROJECT_CUR/VBA/Module1`
    word_plain()               a .doc with no macro storage
    encrypted_ooxml()          an encrypted .docx: `EncryptionInfo` + `EncryptedPackage` streams, no VBA
    many_entries(n)            n empty streams in the root
"""
import struct
from typing import Dict, List, Tuple, Union

Tree = Dict[str, Union[bytes, dict]]

_SECTOR = 512
_MINI_CUTOFF = 4096
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_FATSECT = 0xFFFFFFFD
_NOSTREAM = 0xFFFFFFFF
_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _entry(name: str, kind: int, left: int, right: int, child: int, start: int, size: int) -> bytes:
    raw = name.encode("utf-16-le")
    return struct.pack("<64sHBBIII16sIQQIII", raw.ljust(64, b"\x00"), len(raw) + 2 if name else 0, kind, 1,
                       left, right, child, b"\x00" * 16, 0, 0, 0, start, size, 0)


def _flatten(tree: Tree) -> List[Tuple[str, int, object, int, Tuple[int, int]]]:
    """Entries in id order: (name, kind, payload, child id, (left id, right id)); id 0 is the root."""
    entries: List[list] = [["Root Entry", 5, None, _NOSTREAM, (_NOSTREAM, _NOSTREAM)]]

    def add(children: Tree, parent: int) -> None:
        ids = []
        for name, payload in children.items():
            entries.append([name, 1 if isinstance(payload, dict) else 2, payload, _NOSTREAM, (_NOSTREAM, _NOSTREAM)])
            ids.append(len(entries) - 1)

        def balance(low: int, high: int) -> int:
            if low >= high:
                return _NOSTREAM
            mid = (low + high) // 2
            entries[ids[mid]][4] = (balance(low, mid), balance(mid + 1, high))
            return ids[mid]

        if ids:
            entries[parent][3] = balance(0, len(ids))
        for here in ids:
            if isinstance(entries[here][2], dict):
                add(entries[here][2], here)

    add(tree, 0)
    return [tuple(e) for e in entries]  # type: ignore[misc]


def build_ole(tree: Tree) -> bytes:
    entries = _flatten(tree)
    streams = [(i, bytes(p).ljust(_MINI_CUTOFF, b"\x00") if len(p) else b"")  # type: ignore[arg-type]
               for i, (_, kind, p, _, _) in enumerate(entries) if kind == 2]
    n_dir = -(-len(entries) // 4)
    data_sectors = sum(-(-len(d) // _SECTOR) for _, d in streams)
    n_fat = 1
    while n_fat * 128 < n_fat + n_dir + data_sectors:
        n_fat += 1
    total = n_fat + n_dir + data_sectors
    fat = [_FREESECT] * (n_fat * 128)
    for i in range(n_fat):
        fat[i] = _FATSECT
    first_dir = n_fat
    for i in range(n_dir):
        fat[first_dir + i] = first_dir + i + 1 if i < n_dir - 1 else _ENDOFCHAIN
    cursor = n_fat + n_dir
    placed: Dict[int, Tuple[int, int]] = {}
    body = b""
    for index, data in streams:
        if not data:
            continue
        count = len(data) // _SECTOR if len(data) % _SECTOR == 0 else -(-len(data) // _SECTOR)
        placed[index] = (cursor, len(data))
        for k in range(count):
            fat[cursor + k] = cursor + k + 1 if k < count - 1 else _ENDOFCHAIN
        body += data.ljust(count * _SECTOR, b"\x00")
        cursor += count
    assert cursor == total
    directory = b""
    for index, (name, kind, payload, child, (left, right)) in enumerate(entries):
        start, size = placed.get(index, (_ENDOFCHAIN, 0))
        directory += _entry(name, kind, left, right, child, start, size)
    directory += _entry("", 0, _NOSTREAM, _NOSTREAM, _NOSTREAM, 0, 0) * (n_dir * 4 - len(entries))
    difat = [i for i in range(n_fat)] + [_FREESECT] * (109 - n_fat)
    header = struct.pack("<8s16sHHHHH6sIIIIIIIII", _MAGIC, b"\x00" * 16, 0x3E, 3, 0xFFFE, 9, 6, b"\x00" * 6,
                         0, n_fat, first_dir, 0, _MINI_CUTOFF, _ENDOFCHAIN, 0, _ENDOFCHAIN, 0)
    header += struct.pack("<109I", *difat)
    fat_bytes = struct.pack(f"<{len(fat)}I", *fat)
    return header + fat_bytes + directory + body


_DOC = b"Reformas Exemplo Lda. - bathroom repair invoice 1.048,45 EUR"


def word_with_macros() -> bytes:
    return build_ole({"WordDocument": _DOC, "Macros": {"VBA": {"ThisDocument": b"Sub AutoOpen()"}}})


def excel_with_macros() -> bytes:
    return build_ole({"Workbook": _DOC, "_VBA_PROJECT_CUR": {"VBA": {"Module1": b"Sub Auto_Open()"}}})


def word_plain() -> bytes:
    return build_ole({"WordDocument": _DOC, "1Table": b"table"})


def encrypted_ooxml() -> bytes:
    return build_ole({"EncryptionInfo": b"info", "EncryptedPackage": _DOC})


def many_entries(n: int) -> bytes:
    return build_ole({f"s{i}": b"" for i in range(n)})
