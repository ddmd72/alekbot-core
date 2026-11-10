"""A person referred to only by a relation may take the confirmed list entry's name (fact profile RFC §4.5): code accepts
a name that is not printed when it is a list entry's and the source is printed, and marks the person `from_list`."""
from src.domain.confirmed_people import parse_confirmed_people
from src.domain.drive_grade import TextEvidence
from src.domain.fact_profile_answer import AnswerPerson
from src.domain.fact_profile_derive import check_people

LIST = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex", "Bo"]},
    "people": [{"name": "Maria Doe", "forms": ["Mia"], "relation": "mother"},
               {"name": "Hanna Doe", "forms": [], "relation": "sister"}]})
TEXT = "The user's mother likes tea."


def person(name, role, source):
    return AnswerPerson(name=name, role=role, source=source)


def check(text, people, listed=LIST.entries()):
    drops = {}
    return check_people(people, TextEvidence([text]), drops, listed), drops


def test_a_listed_name_for_a_printed_relation_is_kept_and_marked_from_the_list():
    kept, drops = check(TEXT, [person("Maria Doe", "mother", "mother")])
    assert drops == {}
    assert [(p.name, p.role, p.from_list) for p in kept] == [("Maria Doe", "mother", True)]
    assert kept[0].name_keys and kept[0].stored()["from_list"] is True


def test_the_role_of_a_list_person_is_the_entrys_relation():
    kept, _ = check(TEXT, [person("maria doe", "mom", "mother")])
    assert (kept[0].name, kept[0].role) == ("Maria Doe", "mother")


def test_a_name_printed_in_the_fact_is_not_from_the_list():
    kept, _ = check("Maria Doe likes tea.", [person("Maria Doe", "mother", "Maria Doe")])
    assert kept[0].from_list is False and "from_list" not in kept[0].stored()


def test_a_listed_name_with_an_unprinted_source_is_dropped():
    kept, drops = check(TEXT, [person("Hanna Doe", "sister", "my sister")])
    assert kept == () and drops == {"person_not_printed": 1}


def test_a_name_that_is_not_on_the_list_and_not_printed_is_dropped():
    kept, drops = check(TEXT, [person("Tom Baker", "colleague", "mother")])
    assert kept == () and drops == {"person_not_printed": 1}


def test_the_user_is_never_accepted_without_being_printed():
    kept, drops = check(TEXT, [person("Alex Doe", "", "mother")])
    assert kept == () and drops == {"person_not_printed": 1}


def test_without_a_list_nothing_changes():
    kept, drops = check(TEXT, [person("Maria Doe", "mother", "mother")], listed=())
    assert kept == () and drops == {"person_not_printed": 1}
