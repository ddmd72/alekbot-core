import pytest
from src.locales.uk import get_message, UK_MESSAGES
from src.domain.ui_messages import StatusType

@pytest.mark.requirement("REQ-UI-06")
def test_localization_uk_retrieval():
    """
    Unit test for Ukrainian localization retrieval.
    Covers: REQ-UI-06 (Localization)
    """
    # Test default retrieval
    messages = get_message(StatusType.THINKING)
    assert isinstance(messages, list)
    assert len(messages) > 0
    assert messages == UK_MESSAGES[StatusType.THINKING.value]

    # Test fallback for unknown status (if any)
    # Note: StatusType is an Enum, so we test with a value that might not be in UK_MESSAGES
    # but is a valid StatusType if we were to add one without updating uk.py
    
    # Test overrides
    custom_phrase = "Я дуже сильно думаю..."
    overrides = {StatusType.THINKING.value: [custom_phrase]}
    messages = get_message(StatusType.THINKING, overrides=overrides)
    assert messages == [custom_phrase]

@pytest.mark.requirement("REQ-UI-06")
def test_localization_uk_all_types_present():
    """
    Verify all StatusType values have Ukrainian translations.
    """
    for status in StatusType:
        messages = get_message(status)
        assert messages != ["Обробка..."] or status.value in UK_MESSAGES
