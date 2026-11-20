"""The EmailCardIndex port module (search slice 4): its key-field constant."""


from src.ports.email_card_index import KEY_FIELDS


def test_the_key_fields_are_the_three_card_key_lists():
    assert KEY_FIELDS == ("about_keys", "name_keys", "people_keys")
