"""The capped-filter hedge names the streams that ran (USER_DRIVE_INDEXING_RFC §10.2, §12.3): while the card stream
is off for an embedding-space change, the candidates were name matches only."""
from datetime import datetime, timezone

from src.domain.drive_search import STATUS_OK, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult, render_results

REQUEST = DriveSearchRequest(query="invoice", date_from=datetime(2026, 3, 1, tzinfo=timezone.utc))


def test_refreshing_hedge_says_name_matches_only():
    text = render_results(DriveSearchResult(status=STATUS_OK, request=REQUEST, capped=True, candidates=12,
                                            envelope=DriveSearchEnvelope(refreshing=True)))
    hedge = next(line for line in text.splitlines() if line.startswith("The filters were applied"))
    assert "best 12 name matches only" in hedge and "meaning" not in hedge


def test_normal_hedge_names_both_streams():
    text = render_results(DriveSearchResult(status=STATUS_OK, request=REQUEST, capped=True, candidates=12))
    assert "best 12 name and meaning matches only" in text
