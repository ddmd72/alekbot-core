"""
Apple Books packages (`<name>.epub.pkgf`, docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.1): an EPUB folder the drive
stores as one plain tar. Inside the parser child only, its members are repacked as an EPUB ZIP in memory, so the
normal EPUB path reads it; nothing is written to any path.

The tar is untrusted input. At most MAX_ZIP_MEMBERS headers and MAX_ZIP_UNCOMPRESSED_BYTES of member data, the bounds
an EPUB ZIP gets (`src/utils/document_hardening`), both checked on the headers before any member is read; regular
files only (links, devices, sparse files skipped); each name normalised to a relative ZIP entry name (absolute names,
`..` and AppleDouble `._*` files skipped). A package whose members all sit under one folder is read from that folder.

Public interface:
    PackageError
    is_tar_package(path) -> bool
    epub_zip_from_tar(path) -> io.BytesIO       # raises PackageError, ArchiveTooLargeError
"""
from __future__ import annotations

import io
import tarfile
import zipfile
from typing import List, Optional, Tuple

from src.utils.document_hardening import MAX_ZIP_MEMBERS, MAX_ZIP_UNCOMPRESSED_BYTES, ArchiveTooLargeError

_TAR_BLOCK = 512
_USTAR_AT = 257                      # POSIX ustar and pax headers carry the magic here (the domain sniff's check)
_CONTAINER = "META-INF/container.xml"
_MIMETYPE = "mimetype"
_Entry = Tuple[tarfile.TarInfo, str]


class PackageError(ValueError):
    """The bytes are not a readable tar: the same bytes always fail."""


def is_tar_package(path: str) -> bool:
    """A plain (uncompressed) ustar tar: a compressed one is sniffed `archive` and never reaches the parser."""
    with open(path, "rb") as fh:
        head = fh.read(_TAR_BLOCK)
    return head[_USTAR_AT:_USTAR_AT + 5] == b"ustar"


def epub_zip_from_tar(path: str) -> io.BytesIO:
    """The package's members as a stored EPUB ZIP, `mimetype` first, positioned at its start."""
    buffer = io.BytesIO()
    try:
        with tarfile.open(path, mode="r:") as archive:
            entries = _rooted(_entries(archive))
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as package:
                for member, name in sorted(entries, key=lambda entry: entry[1] != _MIMETYPE):
                    source = archive.extractfile(member)
                    data = source.read(member.size) if source is not None else b""
                    if len(data) != member.size:
                        raise PackageError(f"member data ends early ({len(data)} of {member.size} bytes)")
                    package.writestr(name, data)
    except (tarfile.TarError, EOFError) as exc:
        raise PackageError(f"not a readable tar: {type(exc).__name__}") from exc
    buffer.seek(0)
    return buffer


def _entries(archive: tarfile.TarFile) -> List[_Entry]:
    """The regular files to repack, bounded on their headers alone."""
    found: List[_Entry] = []
    seen = set()
    headers = 0
    declared = 0
    for member in archive:
        headers += 1
        if headers > MAX_ZIP_MEMBERS:
            raise ArchiveTooLargeError(f"more than {MAX_ZIP_MEMBERS} tar members")
        if not member.isfile() or member.issparse():
            continue
        name = _entry_name(member.name)
        if name is None or name in seen:      # a repeated name: the first copy stands
            continue
        declared += member.size
        if declared > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ArchiveTooLargeError(f"more than {MAX_ZIP_UNCOMPRESSED_BYTES} bytes in tar members")
        seen.add(name)
        found.append((member, name))
    return found


def _entry_name(name: str) -> Optional[str]:
    """A relative ZIP entry name, or None for a name that must not be one."""
    if name.startswith("/"):
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or parts[-1].startswith("._"):
        return None
    return "/".join(parts)


def _rooted(entries: List[_Entry]) -> List[_Entry]:
    """Entries relative to the EPUB root: a package tarred with its folder (`Book.epub/META-INF/...`) loses it."""
    names = [name for _, name in entries]
    if _CONTAINER in names or not names or any("/" not in name for name in names):
        return entries
    tops = {name.split("/", 1)[0] for name in names}
    if len(tops) != 1:
        return entries
    return [(member, name.split("/", 1)[1]) for member, name in entries]
