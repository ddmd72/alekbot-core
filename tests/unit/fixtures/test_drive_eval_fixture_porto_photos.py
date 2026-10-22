"""Every photo in the eval fixture's Photos/Porto folder must resolve to
Porto through the bundled place table, or the gold cases place-porto-en / place-porto-ru can never pass."""
import pytest
import json
from pathlib import Path

from src.adapters.geo_place_table import load_place_rows
from src.domain.place_lookup import PlaceIndex

_TREE = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval" / "tree.json"


@pytest.mark.datafile
@pytest.mark.slow
def test_every_porto_photo_resolves_to_porto():
    items = json.loads(_TREE.read_text(encoding="utf-8"))["items"]
    photos = [i for i in items if i["path"].startswith("Photos/Porto/") and not i.get("folder")]
    assert len(photos) == 6
    index = PlaceIndex(load_place_rows())
    places = {i["path"]: index.lookup(i["facets"]["location"]["latitude"], i["facets"]["location"]["longitude"])
              for i in photos}
    assert places == {p: "Porto, Portugal" for p in places}
