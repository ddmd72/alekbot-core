"""`one_call_owner`: the value a one-call marker carries to name its call; a keyed digest of the ticket, so the marker
never holds the ticket itself (SECRETS_AT_REST_RFC §2.5)."""
from src.domain.voice_ticket_keys import button_end_key, call_kind_key, one_call_owner, ticket_key

KEY = b"k" * 32
TICKET = "ticket-a1"


def test_the_owner_is_deterministic_and_names_one_ticket():
    assert one_call_owner(KEY, TICKET) == one_call_owner(KEY, TICKET)
    assert one_call_owner(KEY, TICKET) != one_call_owner(KEY, "ticket-a2")
    assert one_call_owner(KEY, TICKET) != one_call_owner(b"j" * 32, TICKET)


def test_the_owner_holds_no_ticket_and_opens_no_record():
    owner = one_call_owner(KEY, TICKET)
    assert TICKET not in owner
    digests = {key.split(":", 1)[1] for key in (ticket_key(KEY, TICKET), call_kind_key(KEY, TICKET),
                                                  button_end_key(KEY, TICKET))}
    assert owner not in digests
