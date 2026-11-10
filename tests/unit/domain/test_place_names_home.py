"""The owner's home town from the free-text location (fact profile RFC §5.1, §5.3): `resolve_home` over the comma parts
of the location, on a fixture atlas of the invented universe."""
from src.domain.fact_search_rank import HomePlace
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames, resolve_home

PT = ("Portugal",)
SHELBYVILLE = PlaceRow(41.2, -8.5, "Shelbyville", "Shelbyville", "Portugal", 150_000, country_names=PT,
                       geonameid="9100002")
SPRINGFIELD = PlaceRow(41.25, -8.55, "Springfield", "Springfield", "Portugal", 20_000, country_names=PT,
                       large=SHELBYVILLE, geonameid="9100001")
PORTO = PlaceRow(41.15, -8.61, "Porto", "Porto", "Portugal", 230_000, alt_names=("Oporto",), country_names=PT,
                 geonameid="9100003")
COIMBRA = PlaceRow(40.2, -8.42, "Coimbra", "Coimbra", "Portugal", 140_000, country_names=PT, geonameid="9100004")
ROME = PlaceRow(41.9, 12.5, "Rome", "Roma", "Italy", 2_800_000, country_names=("Italia", "Italy"), geonameid="100")
NAMES = PlaceNames([SPRINGFIELD, SHELBYVILLE, PORTO, COIMBRA, ROME])
AT_SPRINGFIELD = HomePlace(keys=frozenset({"pl:9100001", "pl:9100002"}), lp="lp:springfield")


def test_city_then_country():
    assert resolve_home(NAMES, "Springfield, Portugal") == AT_SPRINGFIELD


def test_the_city_need_not_come_first():
    assert resolve_home(NAMES, "Portugal, Springfield") == AT_SPRINGFIELD


def test_a_city_without_a_country():
    assert resolve_home(NAMES, "Springfield") == AT_SPRINGFIELD


def test_brackets_and_slashes_split_like_commas():
    assert resolve_home(NAMES, "Springfield (Portugal)") == AT_SPRINGFIELD
    assert resolve_home(NAMES, "Portugal/Springfield") == AT_SPRINGFIELD


def test_digits_are_stripped_from_the_parts():
    assert resolve_home(NAMES, "12 Oak Street, 0000-000 Springfield, Portugal") == AT_SPRINGFIELD


def test_the_first_part_that_resolves_wins():
    assert resolve_home(NAMES, "Coimbra, Springfield, Portugal") == HomePlace(frozenset({"pl:9100004"}), "lp:coimbra")


def test_nothing_resolving_keeps_only_the_first_parts_local_key():
    assert resolve_home(NAMES, "Lisbon, Portugal") == HomePlace(keys=frozenset(), lp="lp:lisbon")


def test_no_location_or_no_name_at_all_is_no_home():
    assert resolve_home(NAMES, "") is None
    assert resolve_home(NAMES, "0000-000") is None
