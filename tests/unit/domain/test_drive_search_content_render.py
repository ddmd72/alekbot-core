"""render_results and the envelope with content search (USER_DRIVE_INDEXING_RFC §10.2, §10.4, §24.6, §24.10): per-result
facts stay, the standing guidance lives in Smart's protocol token, a window is rendered inside its card's wrapper and
cannot close it, and `content_search` / `unread` read the policy at call time."""
from datetime import datetime, timezone


from src.domain import drive_index_policy as policy
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState, TriageVerdict
from src.domain.drive_search import (
    STATUS_FOLDER_NOT_FOUND, STATUS_OK, DriveSearchCard, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult,
    content_admitted, is_unread, render_results,
)

NOW = datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)
REQUEST = DriveSearchRequest(query="termination")


def card(**kw):
    fields = dict(ref="drive:f1", item_id="f1", name="Scan_0012.pdf", path="Scans/Scan_0012.pdf", kind="pdf",
                  is_folder=False, summary="Scan_0012.pdf | PDF document | contract", nonce="n0000001")
    return DriveSearchCard(**{**fields, **kw})


def render(*cards, **env):
    return render_results(DriveSearchResult(status=STATUS_OK, cards=cards, request=REQUEST,
                                            envelope=DriveSearchEnvelope(index_as_of=NOW, **env)))


class TestFactsStayGuidanceMoves:
    def test_index_time_is_a_fact_without_the_instruction(self):
        text = render(card())
        assert "The index reflects the drive as of 2026-10-12 09:30 UTC." in text
        assert "open the file" not in text and "before answering" not in text

    def test_stale_is_a_fact_without_the_instruction(self):
        text = render(card(stale=True))
        assert "stale: the file changed after it was indexed" in text and "before quoting" not in text

    def test_building_is_a_fact_without_the_instruction(self):
        text = render(building=True)
        assert "The index is still being built" in text
        assert "do not say" not in text and "offer to list" not in text
        missing = render_results(DriveSearchResult(status=STATUS_FOLDER_NOT_FOUND, request=REQUEST,
                                                   envelope=DriveSearchEnvelope(building=True)))
        assert missing.startswith("The index is still being built")

    def test_per_result_facts_stay(self):
        text = render(card(truncated=True, also_at=("Old/Scan_0012.pdf",)), unread=3, reconnect_needed=True)
        assert "truncated: only the beginning of the file was indexed" in text
        assert "identical copies also at: Old/Scan_0012.pdf" in text
        assert "3 documents could not be read" in text and "reconnect the drive" in text

    def test_content_search_drops_the_names_only_line(self):
        assert "names and metadata" in render(card())
        assert "names and metadata" not in render(card(), content_search=True)


class TestRefreshing:
    def test_refreshing_with_content_says_exact_terms_still_ran(self):
        text = render(content_search=True, refreshing=True)
        assert "being refreshed" in text and "exact numbers and codes" in text


class TestPolicyAtCallTime:
    def test_content_admitted_follows_the_policy(self, monkeypatch):
        assert content_admitted()   # the flip (§24.1)
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
        monkeypatch.setattr(policy, "TRIAGED_KINDS", frozenset())
        assert not content_admitted()
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset({"pdf"}))
        assert content_admitted()
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
        monkeypatch.setattr(policy, "TRIAGED_KINDS", frozenset({"image"}))
        assert content_admitted()

    def test_is_unread_reads_the_policy_now(self, monkeypatch):
        pending = DriveIndexRecord(user_id="u", connection_id="u", item_id="p", kind="pdf", state=RecordState.PENDING)
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
        assert not is_unread(pending)
        monkeypatch.setattr(policy, "ADMITTED_KINDS", policy.SLICE2_CONTENT_KINDS)
        assert is_unread(pending)
        assert not is_unread(DriveIndexRecord(user_id="u", connection_id="u", item_id="c", kind="code", state=RecordState.FAILED))

    def test_images_count_only_once_triage_said_text(self, monkeypatch):
        monkeypatch.setattr(policy, "TRIAGED_KINDS", policy.SLICE2_TRIAGED_KINDS)
        image = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind="image", state=RecordState.PENDING)
        assert not is_unread(image)                                        # not triaged yet
        assert is_unread(DriveIndexRecord(user_id="u", connection_id="u", item_id="t", kind="image", state=RecordState.PENDING,
                                          triage_verdict=TriageVerdict.TEXT))
        assert not is_unread(DriveIndexRecord(user_id="u", connection_id="u", item_id="ph", kind="image",
                                              state=RecordState.METADATA_ONLY, reason=Reason.PHOTOGRAPH,
                                              triage_verdict=TriageVerdict.PHOTO))

    def test_an_explicit_set_still_wins(self):
        rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="f", kind="pdf", state=RecordState.FAILED)
        assert is_unread(rec, frozenset({"pdf"})) and not is_unread(rec, frozenset())
