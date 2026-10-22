"""The `transcribe` usage class (USER_DRIVE_INDEXING_RFC §27.10, D46; plan G8): its own hard cap, reservations beside
the spent dollars, and no initial-build allowance."""
from datetime import datetime, timezone

import pytest

from src.domain.drive_enrich_plan import available_usd, enrich_context
from src.domain.drive_enrichment import USD_CAPS, InitialBuild, ModelUsageMonth, UsageClass
from src.domain.drive_index_params import TRANSCRIBE_USD_PER_MONTH
from src.domain.drive_usage import added_usage, reserved_field, reserved_usage

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def test_transcribe_cap_in_usd_caps():
    assert USD_CAPS[UsageClass.TRANSCRIBE.value] == TRANSCRIBE_USD_PER_MONTH == 20.0


def test_initial_build_never_covers_transcribe():
    build = InitialBuild(allowance_usd=40.0)
    ctx = enrich_context(NOW, ModelUsageMonth(transcribe_usd=5.0), build)
    assert available_usd(ctx, "transcribe") == 15.0           # the open build's $40 adds nothing
    assert available_usd(ctx, "passage") == 40.0 + 20.0       # other classes keep it (passage cap $20)


def test_transcribe_dollars_count_toward_the_cap_and_reservations_are_apart():
    usage = added_usage(ModelUsageMonth(), "2026-10-21", "transcribe", 0.5, 0.0, 1)
    usage = reserved_usage(usage, "transcribe", 2.0)
    assert (usage.transcribe_usd, usage.transcribe_reserved_usd, usage.cap_usd("transcribe")) == (0.5, 2.0, 0.5)
    assert reserved_usage(usage, "transcribe", -2.0).transcribe_reserved_usd == 0.0


def test_only_transcribe_has_reservations():
    assert reserved_field("transcribe") == "transcribe_reserved_usd"
    with pytest.raises(ValueError):
        reserved_field("profile")


@pytest.mark.datafile
def test_cabinet_lists_transcribe():
    from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts
    html, dicts = _dicts()
    assert '"passage", "transcribe"]' in html
    for lang in LANGS:
        assert dicts[lang].get("drive.index_usd_transcribe"), lang
        if lang != "en":
            assert dicts[lang]["drive.index_usd_transcribe"] != dicts["en"]["drive.index_usd_transcribe"]
