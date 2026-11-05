"""S5b: the main service's group may call `media` ops on the light service (it probes edit-video sources)."""
from src.composition.sandbox_setup import LIGHT_GROUP_PREFIXES


def test_the_main_group_gets_media_next_to_parse():
    assert {"parse", "media"} <= LIGHT_GROUP_PREFIXES["MAIN"]
