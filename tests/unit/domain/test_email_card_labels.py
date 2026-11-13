"""The card's closed sets, caps and value types (email index v2 §2.1; card slice 1): the mail-only doc types and their
own names table (outside the drive keys digest), the lean caps under the drive's, the card states, and the mail's
code fields."""
from datetime import date, datetime, timezone

from src.domain.drive_index_params import (
    ENTITY_FORMS_MAX, PROFILE_DATES_MAX, PROFILE_ENTITIES_MAX, PROFILE_KEY_VALUES_MAX, PROFILE_TOPICS_MAX,
    PROFILE_WORD_LIST_MAX,
)
from src.domain.drive_labels import DOC_TYPE_NAMES, DOC_TYPES
from src.domain.email_card import AttachmentRead, CardAttachment, CardState
from src.domain.email_card_labels import CARD_DOC_TYPES, MAIL_DOC_TYPE_NAMES, MAIL_DOC_TYPES, card_type_names
from src.domain.email_card_params import (
    MAIL_DATES_MAX, MAIL_ENTITIES_MAX, MAIL_ENTITY_FORMS_MAX, MAIL_KEY_VALUES_MAX, MAIL_PEOPLE_MAX, MAIL_TOPICS_MAX,
    MAIL_WORD_LIST_MAX,
)
from tests.unit.domain.email_card_fixtures import mail


def test_the_card_doc_types_are_the_drives_plus_the_mail_only_ones_with_other_last():
    assert MAIL_DOC_TYPES == ("order_update", "appointment", "account_notice", "message", "invitation")
    assert set(CARD_DOC_TYPES) == set(DOC_TYPES) | set(MAIL_DOC_TYPES)
    assert len(CARD_DOC_TYPES) == len(set(CARD_DOC_TYPES)) and CARD_DOC_TYPES[-1] == "other"
    assert not set(MAIL_DOC_TYPES) & set(DOC_TYPES)


def test_every_mail_only_type_has_names_and_the_drive_table_is_not_touched():
    assert set(MAIL_DOC_TYPE_NAMES) == set(MAIL_DOC_TYPES)
    assert all(len(names) >= 4 and len(set(names)) == len(names) for names in MAIL_DOC_TYPE_NAMES.values())
    assert not set(MAIL_DOC_TYPES) & set(DOC_TYPE_NAMES)        # the drive keys digest stays as it is


def test_card_type_names_reads_the_right_table():
    assert card_type_names("appointment") == MAIL_DOC_TYPE_NAMES["appointment"]
    assert card_type_names("invoice") == tuple(DOC_TYPE_NAMES["invoice"])
    assert card_type_names("other") == () and card_type_names("unknown_type") == ()


def test_the_mail_caps_never_pass_the_drives():
    assert MAIL_ENTITIES_MAX <= PROFILE_ENTITIES_MAX and MAIL_KEY_VALUES_MAX <= PROFILE_KEY_VALUES_MAX
    assert MAIL_DATES_MAX <= PROFILE_DATES_MAX and MAIL_TOPICS_MAX <= PROFILE_TOPICS_MAX
    assert (MAIL_ENTITY_FORMS_MAX, MAIL_WORD_LIST_MAX, MAIL_PEOPLE_MAX) == (3, 12, 6)
    assert MAIL_ENTITY_FORMS_MAX <= ENTITY_FORMS_MAX and MAIL_WORD_LIST_MAX <= PROFILE_WORD_LIST_MAX


def test_the_card_states():
    assert [s.value for s in CardState] == ["pending", "carded", "empty", "abandoned", "source_gone"]


def test_the_sent_day_is_read_in_the_owners_zone():
    late = mail(sent_at=datetime(2025, 6, 1, 23, 30, tzinfo=timezone.utc))
    assert late.sent_day == date(2025, 6, 2)                    # Lisbon is UTC+1 in June
    assert mail(sent_at=datetime(2025, 6, 1, 23, 30, tzinfo=timezone.utc), zone="UTC").sent_day == date(2025, 6, 1)


def test_attachment_read_outcomes():
    assert AttachmentRead.skipped("image") == "skipped:image"
    assert CardAttachment("a.pdf", "pdf", 3_000, AttachmentRead.TEXT.value).read_ok
    assert CardAttachment("a.pdf", "pdf", 3_000, AttachmentRead.READ_BY_READER.value).read_ok
    for read in (AttachmentRead.SCAN_NOT_READ.value, AttachmentRead.LOCKED.value, AttachmentRead.skipped("vcf")):
        assert not CardAttachment("a.pdf", "pdf", 3_000, read).read_ok
