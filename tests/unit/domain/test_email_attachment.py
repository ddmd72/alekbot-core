"""EmailAttachment / AttachmentRead: the stored `read` value and the record's refusals."""
import dataclasses

import pytest

from src.domain.email_attachment import (
    SKIP_REASONS, AttachmentRead, EmailAttachment,
)


def _att(read, **kw):
    return EmailAttachment(name="LAB-2025-03-0417.pdf", kind="pdf", size=20_000, read=read, **kw)


@pytest.mark.parametrize("read", [AttachmentRead.TEXT, AttachmentRead.READ_BY_READER, AttachmentRead.SCAN_NOT_READ,
                                  AttachmentRead.LOCKED])
def test_outcome_of_a_non_skipped_file_is_the_enum_value(read):
    assert _att(read).outcome == read.value


@pytest.mark.parametrize("reason", sorted(SKIP_REASONS))
def test_outcome_of_a_skipped_file_names_the_reason(reason):
    assert _att(AttachmentRead.SKIPPED, reason=reason).outcome == f"skipped:{reason}"


def test_a_skipped_file_needs_a_known_reason():
    with pytest.raises(ValueError):
        _att(AttachmentRead.SKIPPED)
    with pytest.raises(ValueError):
        _att(AttachmentRead.SKIPPED, reason="whatever")


def test_a_reason_without_skipped_is_refused():
    with pytest.raises(ValueError):
        _att(AttachmentRead.TEXT, reason="image")


def test_text_without_a_text_outcome_is_refused():
    with pytest.raises(ValueError):
        _att(AttachmentRead.LOCKED, text="x")
    assert _att(AttachmentRead.TEXT, text="x").text == "x"


def test_the_record_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _att(AttachmentRead.LOCKED).name = "other.pdf"  # type: ignore[misc]
