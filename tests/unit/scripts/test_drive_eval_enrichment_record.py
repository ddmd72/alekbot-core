"""Recording the enrichment snapshot (USER_DRIVE_INDEXING_RFC §25.7, F-22; enrichment_record.py): each recording port
forwards only what the snapshot does not hold yet (one paid call per distinct input), stores what the model answered
(never a transient outage), answers bulk members from their template, and stops at the cost cap."""
from datetime import date

import pytest

from scripts.validation.drive_eval.enrichment_record import (
    CostCapReached, CostMeter, RecordingPassages, RecordingProfiler, RecordingReader, RecordingTriage,
)
from scripts.validation.drive_eval.enrichment_replay import ReplayPassages, ReplayProfiler, ReplayReader, ReplayTriage
from scripts.validation.drive_eval.enrichment_snapshot import EnrichmentSnapshot
from src.domain.drive_enrichment import DriveProfile, HintItem, PhotoDescription, ProfileDate
from src.domain.drive_enrichment_calls import (
    HintReply, HintRequest, ModelReplyStatus, ProfileInput, ProfileReply, ProfileRequest, TriageReply,
    TriageResultV2,
)
from src.domain.drive_extraction import READER, ExtractionOutcome, ExtractionRequest, ExtractionStatus, TriageImage
from src.domain.drive_index import TriageVerdict

S = ModelReplyStatus
PROFILE = DriveProfile(title="Lab report", what_it_is="Blood test", language="es", doc_type="medical_record",
                       dates=(ProfileDate(label="issued", iso=date(2025, 3, 14)),))


def _req(item="i1", head="Colesterol total 212"):
    return ProfileRequest(user_id="u", account_id="a", item_id=item, kind="pdf", name="lab.pdf",
                          input=ProfileInput(head=head))


class Profiler:
    def __init__(self, status=S.OK, usd=0.001):
        self.calls, self.status, self.usd = [], status, usd

    async def profile(self, request):
        self.calls.append(request)
        return ProfileReply(status=self.status, profile=PROFILE if self.status is S.OK else None, model="gpt-6-luna",
                            usd=self.usd)


async def test_a_profile_is_recorded_once_and_replays_the_same():
    snap, inner, meter = EnrichmentSnapshot(), Profiler(), CostMeter(cap_usd=1.0)
    rec = RecordingProfiler(inner, snap, meter, path_of=lambda i: f"Health/{i}.pdf")
    first = await rec.profile(_req())
    again = await rec.profile(_req(item="i2"))      # the same input under another item: no second call
    assert len(inner.calls) == 1 and first.profile == PROFILE and again.profile == PROFILE
    assert meter.spent == pytest.approx(0.001)
    replayed = await ReplayProfiler(snap).profile(_req())
    assert replayed.status is S.OK and replayed.profile == PROFILE
    (entry,) = snap.profiles.values()
    assert entry["path"] == "Health/i1.pdf"


async def test_a_transient_outage_is_not_recorded():
    snap = EnrichmentSnapshot()
    rec = RecordingProfiler(Profiler(status=S.UNAVAILABLE, usd=0.0), snap, CostMeter(cap_usd=1.0))
    reply = await rec.profile(_req())
    assert reply.status is S.UNAVAILABLE and snap.profiles == {}


async def test_a_refusal_is_recorded_and_replays_as_a_refusal():
    snap = EnrichmentSnapshot()
    await RecordingProfiler(Profiler(status=S.REFUSED, usd=0.0), snap, CostMeter(cap_usd=1.0)).profile(_req())
    assert (await ReplayProfiler(snap).profile(_req())).status is S.REFUSED


async def test_bulk_members_are_answered_by_their_template_with_no_call():
    snap, inner = EnrichmentSnapshot(), Profiler()
    rec = RecordingProfiler(inner, snap, CostMeter(cap_usd=1.0), bulk_of=lambda i: ("invoice_2024", 7),
                            bulk_profile=lambda t, n: PROFILE)
    reply = await rec.profile(_req())
    assert reply.profile == PROFILE and inner.calls == [] and snap.profiles == {}


async def test_the_cost_cap_stops_the_recording():
    rec = RecordingProfiler(Profiler(usd=0.2), EnrichmentSnapshot(), CostMeter(cap_usd=0.3))
    await rec.profile(_req(head="one"))
    with pytest.raises(CostCapReached):
        await rec.profile(_req(head="two"))


class Passages:
    def __init__(self):
        self.calls = []

    async def hints(self, request):
        self.calls.append([i for i, _ in request.chunks])
        items = {i: (HintItem(text=f"kw{i}", type="keyword", evidence=t[:5]),) for i, t in request.chunks}
        return HintReply(status=S.OK, items_by_index=items, model="gpt-6-luna", usd=0.0004 * len(request.chunks),
                         outcome_by_index={i: S.OK for i, _ in request.chunks})


def _hreq(chunks):
    return HintRequest(user_id="u", account_id="a", item_id="i", chunks=chunks, title="t", what_it_is="w")


async def test_passages_forward_only_the_chunks_not_recorded_yet():
    snap, inner, meter = EnrichmentSnapshot(), Passages(), CostMeter(cap_usd=1.0)
    rec = RecordingPassages(inner, snap, meter)
    await rec.hints(_hreq([(0, "alpha text"), (1, "beta text")]))
    reply = await rec.hints(_hreq([(0, "alpha text"), (1, "gamma text")]))
    assert inner.calls == [[0, 1], [1]]
    assert reply.items_by_index[0][0].text == "kw0" and reply.items_by_index[1][0].text == "kw1"
    replay = await ReplayPassages(snap).hints(_hreq([(5, "gamma text")]))
    assert replay.items_by_index[5][0].text == "kw1" and replay.outcome_by_index[5] is S.OK


async def test_bulk_passages_have_no_keywords_and_no_call():
    snap, inner = EnrichmentSnapshot(), Passages()
    rec = RecordingPassages(inner, snap, CostMeter(cap_usd=1.0), bulk_of=lambda i: ("invoice_2024", 1))
    reply = await rec.hints(_hreq([(0, "INVOICE INV-2024-001")]))
    assert inner.calls == [] and reply.items_by_index == {0: ()} and snap.hints == {}


class Triage:
    def __init__(self):
        self.batches = []

    async def triage(self, user_id, account_id, images):
        self.batches.append([i.item_id for i in images])
        results = []
        refused = []
        for image in images:
            if image.thumbnail == b"refuse":
                refused.append(image.item_id)
            elif image.thumbnail == b"text":
                results.append(TriageResultV2(item_id=image.item_id, verdict=TriageVerdict.TEXT))
            else:
                results.append(TriageResultV2(item_id=image.item_id, verdict=TriageVerdict.PHOTO,
                                              photo=PhotoDescription(description="a square at dusk")))
        return TriageReply(status=S.OK, results=tuple(results), model="gpt-6-luna", usd=0.0001 * len(images),
                           refused_item_ids=tuple(refused))


def _img(item, data, prior=True):
    return TriageImage(item_id=item, thumbnail=data, mime_type="image/jpeg", camera_prior=prior)


async def test_triage_sends_each_distinct_picture_once():
    snap, inner = EnrichmentSnapshot(), Triage()
    rec = RecordingTriage(inner, snap, CostMeter(cap_usd=1.0))
    reply = await rec.triage("u", "a", [_img("a", b"rome"), _img("b", b"rome"), _img("c", b"text", False),
                                        _img("d", b"refuse")])
    assert inner.batches == [["a", "c", "d"]]
    by_id = {r.item_id: r for r in reply.results}
    assert by_id["b"].photo.description == "a square at dusk" and by_id["c"].verdict is TriageVerdict.TEXT
    assert reply.refused_item_ids == ("d",)
    again = await rec.triage("u", "a", [_img("e", b"rome")])
    assert len(inner.batches) == 1 and again.results[0].item_id == "e"
    replay = await ReplayTriage(snap).triage("u", "a", [_img("x", b"refuse"), _img("y", b"rome")])
    assert replay.refused_item_ids == ("x",) and replay.results[0].photo.description == "a square at dusk"


class Reader:
    def __init__(self):
        self.calls = 0

    @property
    def extractor_id(self):
        return READER

    @property
    def version(self):
        return 1

    async def extract(self, request):
        self.calls += 1
        return ExtractionOutcome(status=ExtractionStatus.TEXT, text="Кассовый чек", pages_total=1, pages_sent=1,
                                 extracted_by="reader@1", model_calls=1, model_cost=0.0006)


async def test_the_reader_is_recorded_by_the_file_bytes(tmp_path):
    path = tmp_path / "scan.jpg"
    path.write_bytes(b"jpeg-bytes")
    snap, inner, meter = EnrichmentSnapshot(), Reader(), CostMeter(cap_usd=1.0)
    rec = RecordingReader(inner, snap, meter)
    request = ExtractionRequest(path=str(path), name="scan.jpg", kind="image", mime_type="image/jpeg",
                                user_id="u", account_id="a", max_model_pages=5)
    await rec.extract(request)
    await rec.extract(request)
    assert inner.calls == 1 and meter.spent == pytest.approx(0.0006)
    replayed = await ReplayReader(snap, version=1).extract(request)
    assert replayed.text == "Кассовый чек" and replayed.model_cost == 0.0
