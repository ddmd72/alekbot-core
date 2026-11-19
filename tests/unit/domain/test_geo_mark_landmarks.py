"""Landmarks and areas in a geo mark (GEO_MARKS_RFC §4.1, §4.2, §4.7): the rendered text, the call note, the wire."""
from datetime import datetime, timezone

import pytest

from src.domain.geo_mark import (
    GeoArea, GeoFix, GeoLandmark, GeoMark, GeoPlace, GeoSource, render_call_note, render_mark_text,
)

NOW = datetime(2026, 11, 18, 18, 40, tzinfo=timezone.utc)
ADDRESS = "Main Street, Springfield"
GYM = GeoLandmark("Iron Temple gym", ("gym",), "BESIDE", 20)
CAFE = GeoLandmark("Café Andorinha", ("cafe",), "ACROSS_THE_ROAD", 150)
LIBRARY = GeoLandmark("Example Library", ("library",), "NEAR", 300)
SQUARE = GeoArea("Main Square", "WITHIN")
HEADER = ("Landmarks around the position (from Google Maps, nearest first, with distances from the position; "
          "reference points for where the position is, not stated by the user):")


def _mark(accuracy=5.0, landmarks=(), areas=(), address=ADDRESS, source=GeoSource.SLACK_LINK) -> GeoMark:
    return GeoMark(GeoFix.parse(41.1, -8.6, accuracy, NOW, source), address, tuple(landmarks), tuple(areas))


def _lines(mark: GeoMark):
    return render_mark_text(mark, "UTC").split("\n")


def test_no_landmarks_and_no_areas_render_as_before():
    lines = _lines(_mark())
    assert len(lines) == 5
    assert lines[2] == "Position: 41.10000, -8.60000, accurate to about 5 m"
    assert lines[3] == f"Nearest street address: {ADDRESS} (looked up from the position, not stated by the user)"


def test_landmarks_follow_the_address_in_order_with_kinds_distance_and_relation():
    lines = _lines(_mark(landmarks=[GYM, CAFE]))
    assert lines[4] == HEADER
    assert lines[5] == "- Iron Temple gym (gym), 20 m away: the position is beside it"
    assert lines[6] == "- Café Andorinha (cafe), 150 m away: the position is across the road from it"
    assert lines[7].startswith("This records the position")


@pytest.mark.parametrize("relation,phrase", [
    ("NEAR", "near it"), ("BESIDE", "beside it"), ("BEHIND", "behind it"),
    ("ACROSS_THE_ROAD", "across the road from it"), ("DOWN_THE_ROAD", "down the road from it"),
    ("AROUND_THE_CORNER", "around the corner from it"),
])
def test_every_relation_has_its_phrase(relation, phrase):
    line = _lines(_mark(landmarks=[GeoLandmark("Example Library", ("library",), relation, 300)]))[5]
    assert line == f"- Example Library (library), 300 m away: the position is {phrase}"


def test_a_within_landmark_has_no_distance():
    line = _lines(_mark(landmarks=[GeoLandmark("Example Library", ("library",), "WITHIN", 300)]))[5]
    assert line == "- Example Library (library): the position is within it"


def test_kinds_absent_and_relation_none_are_left_out():
    line = _lines(_mark(landmarks=[GeoLandmark("Example Library", (), None, 300)]))[5]
    assert line == "- Example Library, 300 m away"
    assert _lines(_mark(landmarks=[GeoLandmark("Example Library", (), "UNKNOWN", 300)]))[5] == line


def test_several_kinds_are_comma_joined():
    line = _lines(_mark(landmarks=[GeoLandmark("Example Library", ("library", "book store"), None, 300)]))[5]
    assert line == "- Example Library (library, book store), 300 m away"


@pytest.mark.parametrize("containment,phrase", [("WITHIN", "within"), ("OUTSKIRTS", "on the outskirts of"),
                                                ("NEAR", "near")])
def test_every_containment_has_its_phrase(containment, phrase):
    lines = _lines(_mark(areas=[GeoArea("Main Square", containment)]))
    assert lines[4] == f"Areas (from Google Maps): the position is {phrase} Main Square"


def test_areas_are_semicolon_joined_and_an_unknown_containment_is_skipped():
    lines = _lines(_mark(areas=[SQUARE, GeoArea("Old Town", "NEAR"), GeoArea("Nowhere", "ELSEWHERE")]))
    assert lines[4] == "Areas (from Google Maps): the position is within Main Square; near Old Town"
    assert "Nowhere" not in "\n".join(lines)


def test_only_unknown_containments_give_no_areas_line():
    assert len(_lines(_mark(areas=[GeoArea("Nowhere", "ELSEWHERE")]))) == 5


def test_landmarks_come_before_areas():
    lines = _lines(_mark(landmarks=[GYM], areas=[SQUARE]))
    assert lines[4] == HEADER and lines[6].startswith("Areas (from Google Maps)")


APPROX_PLAIN = ", approximate: within about 250 m (or 0.3 km), so the address names the area, not the exact spot"
APPROX_PLACES = (", approximate: within about 250 m (or 0.3 km) of this point; everything below is looked up from "
                 "this point, so it names the area, not the exact spot")


def test_approximate_accuracy_keeps_its_wording_without_places():
    assert _lines(_mark(accuracy=250))[2] == "Position: 41.10000, -8.60000" + APPROX_PLAIN


@pytest.mark.parametrize("extra", [{"landmarks": [GYM]}, {"areas": [SQUARE]}])
def test_approximate_accuracy_has_its_second_variant_with_landmarks_or_areas(extra):
    assert _lines(_mark(accuracy=250, **extra))[2] == "Position: 41.10000, -8.60000" + APPROX_PLACES


def test_precise_accuracy_is_unchanged_by_places():
    assert _lines(_mark(landmarks=[GYM], areas=[SQUARE]))[2] == "Position: 41.10000, -8.60000, accurate to about 5 m"


def test_call_note_without_places_is_unchanged():
    assert render_call_note(_mark()) == (
        "[The caller has just shared their position at that moment, on purpose, from the location link in Slack: "
        f"41.10000, -8.60000, accurate to about 5 m; nearest street address: {ADDRESS} (looked up from the position, "
        "not stated by the caller). They shared it for what they ask with it. Do not read the coordinates aloud.]")


def test_call_note_carries_both_clauses():
    within = GeoLandmark("Example Library", ("library",), "WITHIN", 300)
    note = render_call_note(_mark(landmarks=[GYM, within, GeoLandmark("Plain", (), None, 40)],
                                  areas=[SQUARE, GeoArea("Old Town", "OUTSKIRTS")]))
    assert ("not stated by the caller); landmarks around the position (from Google Maps, nearest first): "
            "Iron Temple gym (gym), 20 m away, the position is beside it; "
            "Example Library (library), the position is within it; Plain, 40 m away"
            "; areas: the position is within Main Square, on the outskirts of Old Town. They shared it") in note


def test_call_note_approximate_uses_the_second_variant_with_places():
    assert APPROX_PLACES in render_call_note(_mark(accuracy=250, areas=[SQUARE]))
    assert APPROX_PLAIN in render_call_note(_mark(accuracy=250))


def test_wire_round_trip_carries_landmarks_and_areas():
    mark = _mark(landmarks=[GYM, GeoLandmark("Example Library", (), None, 300)], areas=[SQUARE])
    assert GeoMark.from_wire(mark.to_wire()) == mark


def test_an_old_payload_without_the_keys_still_reads():
    wire = _mark().to_wire()
    assert "landmarks" not in wire and "areas" not in wire
    mark = GeoMark.from_wire(wire)
    assert mark.landmarks == () and mark.areas == ()


@pytest.mark.parametrize("key,value", [
    ("landmarks", "nope"), ("landmarks", ["x"]), ("landmarks", [{"name": 3, "distance_m": 1}]),
    ("landmarks", [{"name": "A", "distance_m": "far"}]), ("landmarks", [{"name": "A", "distance_m": True}]),
    ("landmarks", [{"name": "A", "distance_m": 1, "kinds": "gym"}]),
    ("landmarks", [{"name": "A", "distance_m": 1, "kinds": [1]}]),
    ("landmarks", [{"name": "A", "distance_m": 1, "relation": 5}]),
    ("areas", {"name": "A"}), ("areas", ["x"]), ("areas", [{"name": "A"}]), ("areas", [{"name": "A", "containment": 1}]),
])
def test_invalid_landmark_or_area_types_are_refused(key, value):
    wire = _mark().to_wire()
    wire[key] = value
    with pytest.raises(ValueError):
        GeoMark.from_wire(wire)


def test_geo_place_empty_is_empty():
    assert GeoPlace.EMPTY == GeoPlace(None, (), ())
