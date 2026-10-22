"""The answer's connection-state texts are today's DriveOperations texts byte for byte (DRIVE_SPECIALIST_RFC §7.2 row
6): the domain cannot import infrastructure, so the copies are pinned here."""
from src.domain import drive_answer
from src.infrastructure import drive_operations


def test_the_state_texts_are_todays():
    assert drive_answer._NOT_CONNECTED_TEXT == drive_operations.NOT_CONNECTED
    assert drive_answer._NOT_CONFIGURED_TEXT == drive_operations.NOT_CONFIGURED
