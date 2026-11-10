"""The needs-review answer (fact profile RFC §4.6): stored as a flag and checked items, never a failure; old stored
answers read as confident. Invented cast only."""
from src.domain.fact_profile import PROFILE_FIELDS
from src.domain.fact_profile_answer import ProfileAnswer
from src.domain.fact_profile_derive import derive_profile, directive_profile
from src.domain.place_names import PlaceNames

TEXT = "Jamie and Ana go to Example School."
BASE = dict(label_words=[], time_role="timeless", time_points=[], places=[], people=[], tax_or_fine="none",
            user_identity=False, pet=False, state_or_law=False, about_person="none", subject="lifestyle",
            money_flow=False, use_areas=[], kinds=[], sensitivity="ordinary")


def answer(**over):
    return ProfileAnswer.model_validate({**BASE, **over})


def derive(**over):
    return derive_profile(answer(**over), TEXT, None, PlaceNames([])).profile


def item(field, mention):
    return {"field": field, "mention": mention}


def test_needs_review_with_a_printed_mention_stores_the_flag_and_the_item():
    profile = derive(quality="needs_review", review_items=[item("people", "Jamie")])
    stored = profile.stored()
    assert stored["profile_review_needed"] is True
    assert stored["profile_review_items"] == [{"field": "people", "mention": "Jamie"}]
    assert profile.profile_drops == {}


def test_a_mention_the_fact_does_not_print_is_dropped_and_counted():
    profile = derive(quality="needs_review", review_items=[item("people", "Jamie"), item("places", "Lisbon")])
    assert profile.stored()["profile_review_items"] == [{"field": "people", "mention": "Jamie"}]
    assert profile.profile_drops == {"review_mention_not_printed": 1}


def test_items_given_under_confident_are_dropped_and_counted():
    profile = derive(quality="confident", review_items=[item("people", "Jamie"), item("people", "Ana")])
    assert profile.stored()["profile_review_needed"] is False and profile.stored()["profile_review_items"] == []
    assert profile.profile_drops == {"review_items_without_flag": 2}


def test_needs_review_with_no_surviving_item_stays_flagged():
    profile = derive(quality="needs_review", review_items=[item("people", "Tom")])
    assert profile.review_needed is True and profile.review_items == ()
    assert profile.profile_drops == {"review_mention_not_printed": 1}


def test_the_same_open_point_is_kept_once():
    profile = derive(quality="needs_review", review_items=[item("people", "Jamie"), item("people", "jamie"),
                                                            item("time_points", "Jamie")])
    assert [(i.field, i.mention) for i in profile.review_items] == [("people", "Jamie"), ("time_points", "Jamie")]


def test_a_flagged_profile_is_not_a_redo():
    outcome = derive_profile(answer(quality="needs_review", review_items=[]), TEXT, None, PlaceNames([]))
    assert outcome.redo_reason is None


def test_an_answer_stored_before_these_fields_validates_as_confident_with_no_items():
    stored = {k: v for k, v in answer().stored().items() if k not in ("quality", "review_items")}
    old = ProfileAnswer.model_validate(stored)
    assert old.quality == "confident" and old.review_items == []


def test_a_directive_profile_is_unflagged():
    profile = directive_profile()
    assert profile.review_needed is False and profile.review_items == ()
    assert profile.stored()["profile_review_needed"] is False


def test_the_profile_fields_hold_both_review_keys():
    assert {"profile_review_needed", "profile_review_items"} <= PROFILE_FIELDS
