"""R_NOT_SEARCHED names a drive that was not searched (slice 2a §5.3)."""
from src.domain.drive_answer_texts import R_NOT_SEARCHED


def test_a_drive_not_searched_is_one_of_the_things_a_not_found_must_name():
    assert "a drive not searched" in R_NOT_SEARCHED
    assert "the drive Acme was not searched" in R_NOT_SEARCHED


def test_it_offers_to_search_that_drive():
    assert "search that drive" in R_NOT_SEARCHED
