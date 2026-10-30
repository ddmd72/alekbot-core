from src.domain.voice_ticket_keys import abandoned_key, amd_key, call_kind_key, result_key, ticket_key

KEY = b"k" * 32
TICKET = "ticket-a1"


def test_keys_are_distinct_per_purpose():
    keys = {
        ticket_key(KEY, TICKET), call_kind_key(KEY, TICKET), amd_key(KEY, TICKET),
        result_key(KEY, TICKET, "CA0001"), abandoned_key(KEY, TICKET, "CA0001"),
    }
    assert len(keys) == 5


def test_keys_are_deterministic():
    assert ticket_key(KEY, TICKET) == ticket_key(KEY, TICKET)
    assert result_key(KEY, TICKET, "CA0001") == result_key(KEY, TICKET, "CA0001")


def test_keys_depend_on_ticket_call_id_and_secret_key():
    assert ticket_key(KEY, TICKET) != ticket_key(KEY, "ticket-a2")
    assert ticket_key(KEY, TICKET) != ticket_key(b"j" * 32, TICKET)
    assert result_key(KEY, TICKET, "CA0001") != result_key(KEY, TICKET, "CA0002")
    # the ticket/call_id boundary is not ambiguous
    assert result_key(KEY, "ab", "c") != result_key(KEY, "a", "bc")


def test_no_key_contains_the_ticket_or_call_id():
    for key in (ticket_key(KEY, TICKET), call_kind_key(KEY, TICKET), amd_key(KEY, TICKET),
                result_key(KEY, TICKET, "CA0001"), abandoned_key(KEY, TICKET, "CA0001")):
        assert TICKET not in key and "CA0001" not in key
