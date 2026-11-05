"""
The container facts the sandbox's `parse.container_facts` op answers (docs/10_rfcs/EXTERNAL_FILES_RFC.md D13, owner decision
O2 of 2026-11-05, and 2026-11-05 for OLE2): the parser child's reader of a ZIP's or an OLE2 compound file's macro facts.

Sandbox side only, on purpose: the main service's image has neither `olefile` nor a reason to parse hostile containers
(`tests/unit/test_main_image_requirements_closure.py`); it asks the sandbox through `ContainerFactsPort`. Only
`drive_parser/child.py` imports this module, lazily, per op.

Public interface:
    container_facts(local_path) -> dict        the op's body: the ZIP facts, else the OLE2 facts, else `not_zip`
    zip_container_facts(local_path) -> dict
    ole_container_facts(local_path) -> dict
"""
import posixpath
import re
import zipfile
import zlib
from typing import Any, Dict

from src.utils.document_hardening import MAX_ZIP_MEMBERS

# `[Content_Types].xml` of a real OOXML file is a few KiB; the one member `zip_container_facts` inflates, read up to this.
MAX_CONTENT_TYPES_BYTES = 1024 * 1024
# A macro-enabled OOXML main part (`…macroEnabled.main+xml`, `…macroEnabled.12`) or a VBA project part type.
_MACRO_CONTENT_TYPE = re.compile(rb"macroenabled|vbaproject", re.IGNORECASE)
# ODF keeps Basic libraries under `Basic/` and other script languages under `Scripts/`; a document without macros has
# neither folder.
_ODF_MACRO_DIRS = ("Basic/", "Scripts/")


def zip_container_facts(local_path: str) -> Dict[str, Any]:
    """The facts of a ZIP container that EXTERNAL_FILES D13 needs (owner decision O2: run in the parser child of the
    sandbox's `parse.container_facts` op, never in the main service). The result is the op's body
    (`sandbox_parse_result.CONTAINER_RESULT_KEYS`):

        {"status": "not_zip"}                                   no ZIP central directory
        {"status": "too_large", "members": n, "detail": …}      above MAX_ZIP_MEMBERS, or a content-types part above
                                                                MAX_CONTENT_TYPES_BYTES: the facts are not given
        {"status": "error", "detail": …}                        a ZIP the reader cannot read
        {"status": "zip", "members": n, "vba_project": bool, "macro_content_type": bool, "odf_macros": bool}

    Reads the central directory and inflates one member only, `[Content_Types].xml`, bounded. A member named
    `vbaProject.bin` anywhere (OOXML keeps it under `word/`, `xl/`, `ppt/`), a content type naming a macro-enabled part
    or a VBA project, and an ODF `Basic/` or `Scripts/` folder are the three macro facts."""
    if not zipfile.is_zipfile(local_path):
        return {"status": "not_zip"}
    try:
        with zipfile.ZipFile(local_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                return {"status": "too_large", "members": len(infos),
                        "detail": f"{len(infos)} members (limit {MAX_ZIP_MEMBERS})"}
            names = [info.filename for info in infos]
            content_types = next((info for info in infos if info.filename == "[Content_Types].xml"), None)
            macro_content_type = False
            if content_types is not None:
                if content_types.file_size > MAX_CONTENT_TYPES_BYTES:
                    return {"status": "too_large", "members": len(infos), "detail": "content types part too large"}
                with archive.open(content_types) as handle:
                    data = handle.read(MAX_CONTENT_TYPES_BYTES + 1)
                if len(data) > MAX_CONTENT_TYPES_BYTES:
                    return {"status": "too_large", "members": len(infos), "detail": "content types part too large"}
                macro_content_type = _MACRO_CONTENT_TYPE.search(data) is not None
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, NotImplementedError, RuntimeError, ValueError,
            OSError, EOFError) as exc:   # RuntimeError: an encrypted member; NotImplementedError: an unknown compression
        return {"status": "error", "detail": type(exc).__name__}
    return {
        "status": "zip",
        "members": len(names),
        "vba_project": any(posixpath.basename(name).lower() == "vbaproject.bin" for name in names),
        "macro_content_type": macro_content_type,
        "odf_macros": any(name.startswith(_ODF_MACRO_DIRS) for name in names),
    }


# Storage / stream names of a VBA project in an OLE2 file: Word keeps `Macros/VBA/...`, Excel `_VBA_PROJECT_CUR/VBA/...`,
# and the project's own `VBA` storage holds the modules. Compared lower-cased on every path component.
_OLE_VBA_NAMES = frozenset({"macros", "_vba_project_cur", "vba", "_vba_project"})


def container_facts(local_path: str) -> Dict[str, Any]:
    """The `parse.container_facts` body for any file: the ZIP facts, else the OLE2 facts, else `not_zip`
    (owner decision 2026-11-05: legacy .doc/.xls/.ppt macros). The child's op."""
    facts = zip_container_facts(local_path)
    if facts["status"] != "not_zip":
        return facts
    return ole_container_facts(local_path)


def ole_container_facts(local_path: str) -> Dict[str, Any]:
    """The facts of an OLE2 compound file (olefile, imported lazily):

        {"status": "not_zip"}                                   not an OLE2 file either
        {"status": "too_large", "members": n, "detail": ...}    above MAX_ZIP_MEMBERS directory entries
        {"status": "error", "detail": ...}                      an OLE2 file the reader cannot read
        {"status": "ole", "members": n, "vba_storage": bool}

    Reads the directory only, never a stream body. `vba_storage`: any entry path with a component in `_OLE_VBA_NAMES`.
    An encrypted OOXML package (`EncryptionInfo` / `EncryptedPackage`) has none, so it stays a checked, macro-free
    answer exactly as before (it was `not_zip`)."""
    import olefile

    try:
        if not olefile.isOleFile(local_path):
            return {"status": "not_zip"}
        with olefile.OleFileIO(local_path) as ole:
            entries = ole.listdir(streams=True, storages=True)
    except Exception as exc:   # untrusted bytes: olefile raises OSError, struct.error, IndexError, ... on a broken file
        return {"status": "error", "detail": type(exc).__name__}
    if len(entries) > MAX_ZIP_MEMBERS:
        return {"status": "too_large", "members": len(entries),
                "detail": f"{len(entries)} entries (limit {MAX_ZIP_MEMBERS})"}
    return {
        "status": "ole",
        "members": len(entries),
        "vba_storage": any(part.lower() in _OLE_VBA_NAMES for entry in entries for part in entry),
    }
