"""The folder picker's size confirmation (decisions/drive_scope_add_preview.md): Save on a scope picker first asks
POST .../scope/preview; nothing new saves as before, otherwise the foot shows what would enter the index with Add and
Cancel. Cancel saves nothing and keeps the picker and its selection; Add saves; a failed count still allows Add. The
behavioural checks run the page's picker functions under node over the harness of test_cabinet_drive_redesign."""
import json
import re

import pytest

from tests.unit.web.test_cabinet_drive_redesign import LISTING, SCOPE_ROW, _HTML, _run, node

_NEW = {"new": True, "files": {"documents": 3, "images": 0, "recordings": 1, "other": 2}, "total": 6, "folders": 4,
        "capped": False, "large": False}
PREVIEW_PATH = "/api/drive/connections/c1/scope/preview"


def _pick(body: str, preview) -> dict:
    """`preview`: the data the preview route answers, or None for a failing call."""
    answer = ('{ ok: false, data: { error: "down" } }' if preview is None
              else "{ ok: true, data: " + json.dumps(preview) + " }")
    setup = ('const original = respond; respond = (m, p, b) => m === "POST" ? ' + answer + ' : original(m, p, b);')
    data = {"connections": [SCOPE_ROW], "add_kinds": [], "dr_copy": None}
    script = (LISTING + setup + "driveData = " + json.dumps(data) + ';\nawait openDrivePicker("c1", "scope");\n'
              + 'toggleDrivePickerFolder("c1", 1);\n' + body
              + "\nconst p = driveUiOf('c1').picker;\nprocess.stdout.write(JSON.stringify({ calls: calls.filter((c) => c[0] !== 'GET'),"
                " open: p !== null, preview: p ? p.preview : null, sel: p ? p.sel.map((s) => s.id) : null,"
                " foot: els['drive-pk-foot'].innerHTML, toasts, busy: p ? p.busy : null }));")
    return _run(script)


def _put(ids) -> list:
    return ["PUT", "/api/drive/connections/c1/scope", {"whole_drive": False, "folder_ids": ids}]


def _post(ids) -> list:
    return ["POST", PREVIEW_PATH, {"whole_drive": False, "folder_ids": ids}]


@node
def test_save_asks_for_the_preview_first_and_shows_the_counts_without_saving():
    out = _pick('await saveDrivePicker("c1");', _NEW)
    assert out["calls"] == [_post(["f-proj", "f-inv"])]
    assert out["open"] and out["preview"]["total"] == 6
    foot = out["foot"]
    assert "T:drive.pk_add_title" in foot and "T:drive.pk_add_total_many" in foot
    assert "T:drive.pk_add_documents" in foot and "T:drive.pk_add_recordings" in foot and "T:drive.pk_add_other" in foot
    assert "T:drive.pk_add_images" not in foot            # a kind with no files is left out
    assert "T:drive.pk_add_warn" not in foot              # not large
    assert "cancelDrivePickerPreview('c1')" in foot and "saveDrivePicker('c1', true)" in foot


@node
def test_a_large_or_capped_preview_adds_one_warning_line():
    out = _pick('await saveDrivePicker("c1");', {**_NEW, "capped": True, "large": True})
    assert out["foot"].count("T:drive.pk_add_warn") == 1 and "T:drive.pk_add_total_capped" in out["foot"]


@node
def test_nothing_new_saves_directly():
    out = _pick('await saveDrivePicker("c1");', {"new": False})
    assert out["calls"] == [_post(["f-proj", "f-inv"]), _put(["f-proj", "f-inv"])]
    assert not out["open"] and out["toasts"] == [["T:toast.drive_saved", "success"]]


@node
def test_cancel_saves_nothing_and_keeps_the_picker_and_its_selection():
    out = _pick('await saveDrivePicker("c1"); cancelDrivePickerPreview("c1");', _NEW)
    assert out["calls"] == [_post(["f-proj", "f-inv"])]
    assert out["open"] and out["preview"] is None and out["sel"] == ["f-proj", "f-inv"]
    assert "T:drive.pk_add_title" not in out["foot"] and "T:drive.save" in out["foot"]


@node
def test_add_saves_without_counting_again():
    out = _pick('await saveDrivePicker("c1"); await saveDrivePicker("c1", true);', _NEW)
    assert out["calls"] == [_post(["f-proj", "f-inv"]), _put(["f-proj", "f-inv"])]
    assert not out["open"]


@node
def test_a_failed_count_still_allows_add():
    out = _pick('await saveDrivePicker("c1");', None)
    assert out["calls"] == [_post(["f-proj", "f-inv"])] and out["open"]
    assert "T:drive.pk_add_failed" in out["foot"] and "saveDrivePicker('c1', true)" in out["foot"]
    added = _pick('await saveDrivePicker("c1"); await saveDrivePicker("c1", true);', None)
    assert added["calls"][-1] == _put(["f-proj", "f-inv"]) and not added["open"]


@node
def test_a_selection_change_clears_the_preview():
    out = _pick('await saveDrivePicker("c1"); toggleDrivePickerFolder("c1", 0);', _NEW)
    assert out["preview"] is None and "T:drive.pk_add_title" not in out["foot"]


@node
def test_escape_on_the_confirmation_acts_as_cancel():
    out = _pick('await saveDrivePicker("c1"); requestCloseDrivePicker("c1");', _NEW)
    assert out["open"] and out["preview"] is None and out["calls"] == [_post(["f-proj", "f-inv"])]


@node
def test_the_write_picker_never_asks_for_a_preview():
    data = {"connections": [SCOPE_ROW], "add_kinds": [], "dr_copy": None}
    out = _run(LISTING + "driveData = " + json.dumps(data) + ';\nawait openDrivePicker("c1", "write");\n'
               'setDrivePickerWrite("c1", -2); await saveDrivePicker("c1");\n'
               "process.stdout.write(JSON.stringify({ posts: calls.filter((c) => c[0] === 'POST').length }));")
    assert out == {"posts": 0}


def test_every_language_carries_the_new_keys():
    keys = sorted(set(re.findall(r'"(drive\.pk_add_\w+)":', _HTML)))
    assert len(keys) == 14
    assert len(re.findall(r'"drive\.pk_add_title":', _HTML)) == 4
    for key in keys:
        assert len(re.findall(rf'"{re.escape(key)}":', _HTML)) == 4, key
