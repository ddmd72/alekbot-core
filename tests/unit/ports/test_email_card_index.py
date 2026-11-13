"""The EmailCardIndex port (search slice 4): its shape, and that the in-memory adapter is a full implementation."""
import inspect

import pytest

from src.adapters.in_memory_email_card_index import InMemoryEmailCardIndex
from src.ports.email_card_index import KEY_FIELDS, EmailCardIndex


def test_the_port_is_abstract_with_the_five_reads():
    reads = {"find_by_keys", "list_in_months", "find_nearest", "count_carded", "count_uncarded"}
    assert inspect.isabstract(EmailCardIndex) and set(EmailCardIndex.__abstractmethods__) == reads
    with pytest.raises(TypeError):
        EmailCardIndex()  # type: ignore[abstract]


def test_the_key_fields_are_the_three_card_key_lists():
    assert KEY_FIELDS == ("about_keys", "name_keys", "people_keys")


def test_the_in_memory_adapter_implements_every_read():
    assert not inspect.isabstract(InMemoryEmailCardIndex) and isinstance(InMemoryEmailCardIndex(), EmailCardIndex)
