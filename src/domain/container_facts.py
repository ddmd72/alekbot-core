"""
The facts of a ZIP container, as the sandbox's `parse.container_facts` op answers them (docs/10_rfcs/EXTERNAL_FILES_RFC.md
D13, owner decision O2 of 2026-11-05: the macro check runs in the sandbox, never in the main service). Pure.

Two public names on purpose (CLAUDE.md's one-class rule waived): the status vocabulary and the value it qualifies are
read together by the client, the port and `active_kind`.

`ContainerStatus` is the op's own verdict: `ZIP` (the facts are given), `OLE` (a legacy OLE2 compound file: .doc/.xls/.ppt
and encrypted OOXML; the facts are given, owner decision 2026-11-05), `NOT_ZIP` (neither a ZIP nor an OLE2 file),
`TOO_LARGE` (above the member cap or the content-types cap: no facts), `ERROR` (a ZIP the reader could not read, or a run
failure of the op: timeout, memory, an invalid result). Only `ZIP`, `OLE` and `NOT_ZIP` are a checked answer; every other
status means the container was NOT checked, which D13 treats as unsafe (fail closed).

Public interface:
    ContainerStatus
    ContainerFacts(status, members=0, vba_project=False, macro_content_type=False, odf_macros=False,
                   vba_storage=False, detail="")
        .checked -> bool          ZIP, OLE or NOT_ZIP
        .carries_macros -> bool   a ZIP with any of the three macro facts, or an OLE2 file with a VBA storage
    container_facts_from_result(result) -> ContainerFacts      from the op's decoded body (`decode_parse_result`)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ContainerStatus(str, Enum):
    ZIP = "zip"
    OLE = "ole"
    NOT_ZIP = "not_zip"
    TOO_LARGE = "too_large"
    ERROR = "error"


@dataclass(frozen=True)
class ContainerFacts:
    status: ContainerStatus
    members: int = 0
    vba_project: bool = False          # a `vbaProject.bin` member (OOXML: docm, xlsm, pptm, xlsb …)
    macro_content_type: bool = False   # `[Content_Types].xml` names a macro-enabled part or a VBA project
    odf_macros: bool = False           # an ODF `Basic/` or `Scripts/` folder
    vba_storage: bool = False          # an OLE2 `Macros` / `_VBA_PROJECT_CUR` / `VBA` storage (.doc, .xls, .ppt)
    detail: str = ""

    @property
    def checked(self) -> bool:
        return self.status in (ContainerStatus.ZIP, ContainerStatus.OLE, ContainerStatus.NOT_ZIP)

    @property
    def carries_macros(self) -> bool:
        if self.status is ContainerStatus.OLE:
            return self.vba_storage
        return self.status is ContainerStatus.ZIP and (self.vba_project or self.macro_content_type or self.odf_macros)


def container_facts_from_result(result: Mapping[str, Any]) -> ContainerFacts:
    """The value of a decoded `parse.container_facts` body (closed keys and types already checked by
    `decode_parse_result`). ValueError for a status outside `ContainerStatus`."""
    return ContainerFacts(
        status=ContainerStatus(result["status"]),
        members=int(result.get("members", 0)),
        vba_project=bool(result.get("vba_project", False)),
        macro_content_type=bool(result.get("macro_content_type", False)),
        odf_macros=bool(result.get("odf_macros", False)),
        vba_storage=bool(result.get("vba_storage", False)),
        detail=str(result.get("detail", "")),
    )
