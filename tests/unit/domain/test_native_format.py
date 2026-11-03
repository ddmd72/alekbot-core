"""The native-format declaration (EXTERNAL_FILES_RFC §4.3, P5): a frozen value refused above the caps table's native
ceiling, carried in a context as JSON-plain data and read back unchanged."""
import json
from dataclasses import FrozenInstanceError

import pytest

from src.domain.document_limits import MAX_DRIVE_VISION_IMAGE_BYTES, MAX_DRIVE_VISION_PDF_BYTES
from src.domain.native_format import NATIVE_FORMATS_KEY, NativeFormat, from_plain, to_plain

MIB = 1024 * 1024
DECLARED = (
    NativeFormat(mime="image/jpeg", max_bytes=3 * MIB + 768 * 1024, max_pages=1),
    NativeFormat(mime="image/png", max_bytes=5 * MIB, max_pages=1),
    NativeFormat(mime="application/pdf", max_bytes=20 * MIB, max_pages=100),
)


def test_the_key_is_native_formats():
    assert NATIVE_FORMATS_KEY == "native_formats"


def test_the_value_round_trips_through_json():
    wire = json.loads(json.dumps({NATIVE_FORMATS_KEY: to_plain(DECLARED)}))
    assert from_plain(wire[NATIVE_FORMATS_KEY]) == DECLARED


def test_the_plain_form_is_a_list_of_dicts_of_its_three_fields():
    assert to_plain(DECLARED[2:]) == [{"mime": "application/pdf", "max_bytes": 20 * MIB, "max_pages": 100}]


@pytest.mark.parametrize("value", [None, []])
def test_no_key_or_an_empty_list_is_text_only(value):
    assert from_plain(value) == ()
    assert to_plain(()) == []


def test_a_format_is_frozen():
    with pytest.raises(FrozenInstanceError):
        DECLARED[0].max_bytes = 1  # type: ignore[misc]


@pytest.mark.parametrize("mime, at_ceiling", [
    ("image/webp", MAX_DRIVE_VISION_IMAGE_BYTES),
    ("application/pdf", MAX_DRIVE_VISION_PDF_BYTES),
])
def test_a_format_at_its_ceiling_is_accepted_and_one_byte_above_is_refused(mime, at_ceiling):
    assert NativeFormat(mime=mime, max_bytes=at_ceiling, max_pages=1).max_bytes == at_ceiling
    with pytest.raises(ValueError, match="ceiling"):
        NativeFormat(mime=mime, max_bytes=at_ceiling + 1, max_pages=1)


@pytest.mark.parametrize("mime", ["text/plain", "audio/mpeg", "video/mp4", "application/zip",
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document"])
def test_a_kind_never_sent_natively_cannot_be_declared(mime):
    with pytest.raises(ValueError, match="ceiling"):
        NativeFormat(mime=mime, max_bytes=1, max_pages=1)


@pytest.mark.parametrize("max_bytes, max_pages", [(0, 1), (-1, 1), (1, 0), (1, -5)])
def test_caps_below_one_are_refused(max_bytes, max_pages):
    with pytest.raises(ValueError):
        NativeFormat(mime="image/png", max_bytes=max_bytes, max_pages=max_pages)


@pytest.mark.parametrize("max_bytes, max_pages", [(True, 1), (1.5, 1), ("1", 1), (1, None)])
def test_caps_must_be_integers(max_bytes, max_pages):
    with pytest.raises(ValueError):
        NativeFormat(mime="image/png", max_bytes=max_bytes, max_pages=max_pages)


@pytest.mark.parametrize("mime", ["", "Image/PNG", " image/png", "application/pdf; charset=binary", None])
def test_the_mime_must_be_a_bare_lower_case_type(mime):
    with pytest.raises(ValueError):
        NativeFormat(mime=mime, max_bytes=1, max_pages=1)


def test_from_plain_refuses_a_declaration_above_the_ceiling():
    plain = [{"mime": "application/pdf", "max_bytes": MAX_DRIVE_VISION_PDF_BYTES + 1, "max_pages": 10}]
    with pytest.raises(ValueError, match="ceiling"):
        from_plain(plain)


@pytest.mark.parametrize("value", [
    {"mime": "image/png", "max_bytes": 1, "max_pages": 1},          # a dict, not a list
    (to_plain(DECLARED)[0],),                                        # a tuple: not what JSON gives back
    "image/png",
    [DECLARED[0]],                                                   # the dataclass itself, not its plain form
    [{"mime": "image/png", "max_bytes": 1}],                         # a missing field
    [{"mime": "image/png", "max_bytes": 1, "max_pages": 1, "x": 0}],  # an extra field
])
def test_from_plain_refuses_anything_but_its_plain_form(value):
    with pytest.raises(ValueError):
        from_plain(value)
