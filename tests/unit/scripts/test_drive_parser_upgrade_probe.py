"""drive_parser_upgrade_probe.py (U5 Part E): the verdict of a record is pure and built from the core; the script is read-only
(AST guard); the report carries no name, item id or text. Fakes only: no Firestore, no drive, no model, no count_tokens call.
Fixture records are the invented drive-eval files (a lab report PDF, a motor policy PDF, a Porto lease DOCX, a CV)."""
import ast
import asyncio
import importlib.util
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.document_extraction import ExtractionStatus, model_route
from src.domain.drive_chunker import chunk_text
from src.domain.drive_enrichment import ModelUsageMonth
from src.domain.drive_extraction import ExtractionOutcome
from src.domain.drive_index import DriveChunk, DriveIndexRecord, Reason, RecordState
from src.domain.drive_index_params import MAX_CHUNKS_PER_USER, READER_PAGE_USD_ESTIMATE

_PATH = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_parser_upgrade_probe.py"
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
SPACE = "gemini-embedding-2/768"
_WORDS = ("amber basil cedar dune ember fjord glade harbor iris juniper kettle lantern meadow nectar orchard pebble quill "
          "ridge saddle thistle umber violet willow yarrow zephyr anchor beacon canyon delta estuary forest garnet hollow "
          "island jasmine kernel lagoon marble nutmeg oyster prairie quartz raven spruce timber upland valley walnut").split()


def _prose(prefix, n, noun):
    """Varied prose (the machine-text gate must pass): seeded random words, so no two paragraphs repeat."""
    rng = random.Random(7)
    return "\n\n".join(f"{prefix} {i}: " + " ".join(rng.choice(_WORDS) for _ in range(28)) + f" the {noun} pays {i * 37 + 11} euro."
                        for i in range(n))


LEASE = "Lease of the flat at Rua Imaginária 45, 2.º, 4000-123 Porto, between Hanna Doe and the landlord.\n\n" + _prose("Clause", 120, "tenant")
LEASE_CHUNKED = chunk_text(LEASE)


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("drive_parser_upgrade_probe", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stored_of(chunked, item="i1", generation=4):
    return [DriveChunk(user_id="u1", connection_id="conn-1", item_id=item, generation=generation, index=i, text=c.text,
                       char_start=c.char_start, char_end=c.char_end, kind="docx")
            for i, c in enumerate(chunked.chunks)]


def record(chunked=LEASE_CHUNKED, text=LEASE, **kw) -> DriveIndexRecord:
    base = dict(user_id="u1", connection_id="conn-1", item_id="i1", name="lease-porto-hanna-doe.docx", kind="docx",
                state=RecordState.INDEXED, content_version="v7", indexed_version="v7", index_generation=5,
                indexed_generation=4, chunk_space=SPACE, chunk_count=len(chunked.chunks), text_chars=len(text),
                pages_total=0, truncated=False, script_rendered=False, capture="digital", capture_source="detected",
                extracted_by="parser@3")
    base.update(kw)
    return DriveIndexRecord(**base)


def outcome(text=LEASE, **kw) -> ExtractionOutcome:
    base = dict(status=ExtractionStatus.TEXT, text=text, pages_total=0, extracted_by="parser@4", reader_family="docx")   # the parser sets no capture word
    base.update(kw)
    return ExtractionOutcome(**base)


def classify(probe, rec=None, stored=None, out=None, **kw):
    rec = rec or record()
    stored = stored_of(LEASE_CHUNKED) if stored is None else stored
    kw.setdefault("space", SPACE)
    kw.setdefault("readable_total", rec.chunk_count)
    return probe.classify(rec, stored, out or outcome(), **kw)


# -- classify: pure, from the core -----------------------------------------------------------------------------------

def test_an_unchanged_text_is_same_with_no_changed_chunk(probe):
    verdict = classify(probe)
    assert (verdict.state, verdict.changed_chunk_count, verdict.changed_texts) == (probe.SAME, 0, ())
    assert not verdict.profile_job and verdict.hint_chunks == 0 and not verdict.kept_as_parser


def test_one_changed_chunk_counts_one(probe):
    edited = LEASE.replace("Clause 60:", "Clause 6O:")      # same length: the offsets of every chunk stay
    verdict = classify(probe, out=outcome(edited))
    assert verdict.state == probe.CHANGED
    assert verdict.changed_chunk_count == 1 and len(verdict.changed_texts) == 1


def test_the_stored_vectors_of_another_space_are_never_reused(probe):
    verdict = classify(probe, rec=record(chunk_space="other/3072"))
    assert verdict.state == probe.CHANGED and verdict.changed_chunk_count == len(LEASE_CHUNKED.chunks)


def test_a_text_that_trips_the_machine_gate_is_a_demote_flip(probe):
    log = "\n".join("upstream gateway accepted the request and returned status ok to the caller" for _ in range(80))
    verdict = classify(probe, out=outcome(log))
    assert (verdict.state, verdict.reason) == (probe.DEMOTED, Reason.MACHINE_TEXT.value)


def test_a_record_already_demoted_for_that_reason_is_not_a_flip(probe):
    log = "\n".join("upstream gateway accepted the request and returned status ok to the caller" for _ in range(80))
    rec = record(state=RecordState.METADATA_ONLY, reason=Reason.MACHINE_TEXT, indexed_generation=None, chunk_count=0)
    assert classify(probe, rec=rec, stored=[], out=outcome(log)).state == probe.STILL_DEMOTED


def test_no_text_is_a_demote_flip_by_its_reason(probe):
    verdict = classify(probe, out=outcome("", status=ExtractionStatus.NO_TEXT))
    assert (verdict.state, verdict.reason) == (probe.DEMOTED, Reason.NO_TEXT.value)


def test_a_timeout_is_an_error_of_the_file(probe):
    verdict = classify(probe, out=outcome("", status=ExtractionStatus.TIMEOUT))
    assert verdict.state == probe.ERROR


def test_a_chunk_count_above_the_headroom_is_a_chunk_cap_refusal(probe):
    verdict = classify(probe, readable_total=MAX_CHUNKS_PER_USER)
    refused = classify(probe, rec=record(indexed_generation=None, state=RecordState.FAILED, chunk_count=0), stored=[],
                       readable_total=MAX_CHUNKS_PER_USER)
    assert (refused.state, refused.reason, refused.chunk_cap_refused) == (probe.DEMOTED, Reason.USER_CHUNK_CAP.value, True)


def _lab_pdf():
    text = "Lab report LAB-2025-03-0417 for Alex Doe. Hemoglobin within range."          # about 65 characters
    return record(kind="pdf", name="lab.pdf", pages_total=40, text_chars=len(text),
                  chunk_count=1), text, outcome(text, pages_total=40, reader_family="pdf")


def test_a_pdf_whose_new_text_routes_is_kept_as_parser_with_its_pages(probe):
    rec, text, out = _lab_pdf()
    assert model_route(out, "pdf") == "low_density"
    verdict = classify(probe, rec=rec, stored=[], out=out)
    assert verdict.kept_as_parser and verdict.routed_pages == 40 and verdict.state == probe.CHANGED


def test_kept_as_parser_dollars_are_avoided_and_absent_from_the_total(probe):
    rec, _, out = _lab_pdf()
    totals = probe.KindTotals()
    totals.add(classify(probe, rec=rec, stored=[], out=out), tokens=0)
    prices = probe.Prices(0.15, 0.002, 0.001, 0.0006, "estimate")
    report = probe.kind_report(totals, prices, (1, 1))
    assert report["kept_as_parser"] == {"records": 1, "pages": 40}
    assert report["usd"]["reader_avoided"] == pytest.approx(0.024)
    usd = report["usd"]
    assert usd["total"] == pytest.approx(usd["embeddings"] + usd["profile"] + usd["doc2query"])   # no reader dollars in it


def test_an_unrouted_pdf_is_not_kept(probe):
    text = LEASE
    rec = record(kind="pdf", pages_total=2)
    verdict = classify(probe, rec=rec, out=outcome(text, pages_total=2, reader_family="pdf"))
    assert not verdict.kept_as_parser and verdict.routed_pages == 0


def test_a_moved_profile_fingerprint_counts_one_profile_job(probe):
    cv = "Curriculum vitae of Greta Klein, back-end developer.\n\n" + _prose("Role", 60, "designer")
    chunked = chunk_text(cv)
    rec = record(chunked, cv, kind="docx", profile_input_fingerprint="stale-fingerprint")
    stored = stored_of(chunked)
    same = classify(probe, rec=rec, stored=stored, out=outcome(cv))
    assert same.state == probe.SAME and not same.profile_job                 # an equal text cannot move it
    moved = classify(probe, rec=rec, stored=stored, out=outcome(cv.replace("Role 3:", "Role 3b:")))
    assert moved.state == probe.CHANGED
    assert probe.input_fingerprint_of(rec, stored) is not None, "fixture: this kind sends a model input"
    assert moved.profile_job


def test_unhinted_changed_chunks_count_doc2query(probe):
    paragraphs = LEASE.split("\n\n")
    paragraphs[40:80] = _prose("Annex", 40, "landlord").split("\n\n")      # whole chunks of new text: no old hint covers them
    rewritten = "\n\n".join(paragraphs)
    stored = stored_of(LEASE_CHUNKED)
    every = frozenset(c.index for c in stored)
    hinted = classify(probe, out=outcome(rewritten), hinted_indexes=every)
    assert hinted.state == probe.CHANGED and 1 <= hinted.hint_chunks <= hinted.changed_chunk_count
    none = classify(probe, out=outcome(rewritten), hinted_indexes=frozenset())
    assert none.hint_chunks == 0                     # a file with no hint at all has pending hints whatever the parser is


def test_a_changed_chunk_that_reuses_an_old_hint_is_not_counted(probe):
    edited = LEASE.replace("Clause 60:", "Clause 6O:")        # only a few words moved: most paragraphs come from the old chunk
    verdict = classify(probe, out=outcome(edited), hinted_indexes=frozenset(c.index for c in stored_of(LEASE_CHUNKED)))
    assert verdict.changed_chunk_count == 1 and verdict.hint_chunks == 0


# -- sampling, prices, aggregation ------------------------------------------------------------------------------------

def _many(n, kind="pdf"):
    return [record(item_id=f"{kind}-{i:05d}", kind=kind) for i in range(n)]


def test_all_records_are_probed_up_to_the_limit(probe):
    chosen, counts = probe.stratified_sample(_many(10), 10)
    assert len(chosen) == 10 and counts == {"pdf": (10, 10)}


def test_a_larger_set_is_sampled_per_kind_in_proportion_and_deterministically(probe):
    records = _many(900, "pdf") + _many(90, "docx") + _many(10, "eml")
    chosen, counts = probe.stratified_sample(records, 100)
    assert counts["pdf"] == (900, 90) and counts["docx"] == (90, 9) and counts["eml"] == (10, 1)
    assert len(chosen) == 100
    assert [r.item_id for r in chosen] == [r.item_id for r in probe.stratified_sample(records, 100)[0]]


def test_average_call_usd_prices_the_rows_by_billing(probe):
    rows = [{"agent_type": "drive_profiler", "model": "gemini-2.5-flash", "n": 10, "prompt_tokens": 100_000,
             "completion_tokens": 10_000},
            {"agent_type": "drive_passages", "model": "gemini-2.5-flash", "n": 4, "prompt_tokens": 1, "completion_tokens": 1}]
    from src.domain.billing import calculate_cost
    expected = calculate_cost("gemini-2.5-flash", 100_000, 10_000) / 10
    assert probe.average_call_usd(rows, "drive_profiler") == pytest.approx(expected)
    assert probe.average_call_usd(rows, "other") is None


def test_the_reader_price_per_page_is_measured_from_the_ledger_or_the_estimate(probe):
    reads = [(datetime(2026, 10, 3, tzinfo=timezone.utc), 100), (datetime(2025, 1, 1, tzinfo=timezone.utc), 900)]
    assert probe.reader_usd_per_page({"2026-10": 0.1, "2026-09": 0.0}, reads) == (0.001, "measured")
    assert probe.reader_usd_per_page({}, reads) == (READER_PAGE_USD_ESTIMATE, "estimate")


def test_totals_add_embeddings_and_o4_enrichment_but_not_the_avoided_reader(probe):
    totals = probe.KindTotals()
    totals.add(probe.Verdict(probe.CHANGED, changed_chunk_count=3, profile_job=True, hint_chunks=3,
                             kept_as_parser=True, routed_pages=10), tokens=2_000_000)
    prices = probe.Prices(0.20, 0.01, 0.002, 0.0006, "estimate")
    report = probe.build_report({"pdf": totals}, {"pdf": (4, 1)}, prices, sample_limit=1, connections=1, now=NOW)
    usd = report["kinds"]["pdf"]["usd"]
    assert usd["embeddings"] == pytest.approx(0.4) and usd["profile"] == pytest.approx(0.01)
    assert usd["doc2query"] == pytest.approx(0.004)                       # 3 chunks, 2 per call: 2 calls
    assert usd["total"] == pytest.approx(0.414) and usd["reader_avoided"] == pytest.approx(0.006)
    assert report["totals"]["usd_extrapolated"]["total"] == pytest.approx(0.414 * 4)    # a 1-in-4 sample
    assert report["sampled"] is True and report["over_money_rule"] is False


def test_an_unpriced_agent_is_named(probe):
    report = probe.build_report({}, {}, probe.Prices(0.2, None, 0.001, 0.0006, "estimate"), sample_limit=5,
                                connections=1, now=NOW)
    assert report["unpriced"] == ["profile_call_usd"]


# -- the read-only guard ----------------------------------------------------------------------------------------------

_IO_ROOTS = {"repo", "drive", "bq", "genai_client", "extractor"}
_IO_ALLOWED = {"records_page", "readable_chunk_total", "get_chunks_with_vectors", "get_hints", "get_model_usage",
               "download_to_file", "count_tokens", "query", "extract", "aio", "models"}
_DENIED = re.compile(r"^(write_|set_|update|delete|commit|enqueue|upsert|create_|save|insert|put|patch|send|post|add_|remove)")
_LOCAL_OUTPUT = {"write_text", "insert", "add_argument"}   # the JSON report on the local disk, sys.path.insert, argparse


def violations(source: str):
    """Every attribute use on an I/O object outside the read allowlist, and any write-shaped method call anywhere."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, (ast.Attribute, ast.Call)):
                root = root.value if isinstance(root, ast.Attribute) else root.func
            if isinstance(root, ast.Name) and root.id in _IO_ROOTS and node.attr not in _IO_ALLOWED:
                found.append(f"{root.id}.{node.attr}")
            if _DENIED.match(node.attr) and node.attr not in _LOCAL_OUTPUT:
                found.append(node.attr)
    return found


def test_the_script_calls_nothing_outside_the_read_allowlist():
    assert violations(_PATH.read_text(encoding="utf-8")) == []


def test_the_guard_catches_writes():
    assert violations("async def f(repo):\n    await repo.write_chunks([], [])") != []
    assert violations("async def f(drive):\n    await drive.upload(1)") != []
    assert violations("def f(x):\n    x.commit()") != []
    assert violations("def f(repo):\n    repo.upsert_records([])") != []


def test_every_firestore_client_the_script_builds_passes_a_database():
    source = _PATH.read_text(encoding="utf-8")
    assert "firestore.Client(" not in source and "firestore.AsyncClient(" not in source   # only scripts._firestore builds one


# -- the whole run on fakes: nothing but numbers in the output --------------------------------------------------------

class _FakeDrive:
    def __init__(self, blobs):
        self.blobs = blobs
        self.downloads = 0

    async def download_to_file(self, handle, item_id, dest_path, max_bytes):
        self.downloads += 1
        Path(dest_path).write_bytes(self.blobs[item_id])
        return 1


class _FakeExtractor:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    async def extract(self, request):
        return self.outcomes[request.item_id]


async def _count(texts):
    return sum(len(t) // 4 for t in texts), False


def _world():
    repo = InMemoryDriveIndexRepository()
    scope = ConnectionScope("u1", "conn-1")
    lab_text = "Lab report LAB-2025-03-0417 for Alex Doe. Hemoglobin within range."
    lab = record(item_id="item-lab-0417", name="LAB-2025-03-0417.pdf", kind="pdf", pages_total=40, text_chars=len(lab_text),
                 chunk_count=1)
    policy = record(item_id="item-policy-118204", name="MP-2026-118204.pdf", kind="pdf", pages_total=2,
                    capture="", capture_source="")      # a PDF read with no capture signals settles to nothing
    lease = record(item_id="item-lease-porto", name="lease-porto-hanna-doe.docx")
    reader_rec = record(item_id="item-reader", name="scan.pdf", kind="pdf", extracted_by="reader@2", model_read_at=NOW)
    other = record(item_id="item-v4", name="done.docx", extracted_by="parser@4")
    edited = LEASE.replace("Clause 60:", "Clause 6O:")
    outcomes = {"item-lab-0417": outcome(lab_text, pages_total=40, reader_family="pdf"),
                "item-policy-118204": outcome(LEASE, pages_total=2, reader_family="pdf"),
                "item-lease-porto": outcome(edited)}
    blobs = {"item-lab-0417": b"%PDF-1.4\n", "item-policy-118204": b"%PDF-1.4\n", "item-lease-porto": b"PK\x03\x04"}
    return repo, scope, [lab, policy, lease, reader_rec, other], outcomes, blobs


def test_the_run_reports_numbers_only_and_reads_each_parser_record_once(probe):
    repo, scope, records, outcomes, blobs = _world()
    asyncio.run(repo.upsert_records(records))
    for rec in records:
        if rec.indexed_generation is not None and rec.item_id in ("item-policy-118204", "item-lease-porto"):
            chunks = stored_of(LEASE_CHUNKED, item=rec.item_id)
            asyncio.run(repo.write_chunks(chunks, [[0.0]] * len(chunks)))
    drive = _FakeDrive(blobs)
    conn = SimpleNamespace(scope=scope, handle=SimpleNamespace())
    per_kind, counts, reads, failed = asyncio.run(probe.run(
        [(conn, "u1")], repo=repo, drive=drive, extractor=_FakeExtractor(outcomes), space=SPACE, count=_count,
        sample=5000, log=lambda _: None))
    assert failed == 0 and drive.downloads == 3 and counts == {"pdf": (2, 2), "docx": (1, 1)}
    assert reads == [(NOW, 1)]
    prices = probe.Prices(0.2, 0.01, 0.002, 0.0006, "estimate")
    report = probe.build_report(per_kind, counts, prices, sample_limit=5000, connections=1, now=NOW)
    assert report["kinds"]["pdf"]["kept_as_parser"] == {"records": 1, "pages": 40}
    assert report["kinds"]["docx"]["changed"] == 1 and report["kinds"]["pdf"]["same"] == 1
    dump = json.dumps(report) + "\n".join(probe.summary_lines(report))
    for secret in ("LAB-2025-03-0417", "MP-2026-118204", "lease-porto", "hanna", "Hanna", "Alex Doe", "Porto", "item-",
                   "Hemoglobin", "Clause", "u1", "conn-1"):
        assert secret not in dump, secret


def test_a_download_failure_is_an_error_of_that_file_not_of_the_run(probe):
    repo, scope, records, outcomes, blobs = _world()
    asyncio.run(repo.upsert_records(records))
    drive = _FakeDrive({})                             # every download raises KeyError
    conn = SimpleNamespace(scope=scope, handle=SimpleNamespace())
    per_kind, _, _, failed = asyncio.run(probe.run(
        [(conn, "u1")], repo=repo, drive=drive, extractor=_FakeExtractor(outcomes), space=SPACE, count=_count,
        sample=5000, log=lambda _: None))
    assert failed == 0 and sum(t.errors["KeyError"] for t in per_kind.values()) == 3


def test_a_record_with_no_extraction_stamp_is_skipped_and_does_not_crash_the_run(probe):
    repo, scope, records, outcomes, blobs = _world()
    unstamped = record(item_id="item-unstamped", name="never-extracted.docx", extracted_by=None)
    asyncio.run(repo.upsert_records(records + [unstamped]))
    asyncio.run(repo.write_chunks(stored_of(LEASE_CHUNKED, item="item-lease-porto"), [[0.0]] * len(LEASE_CHUNKED.chunks)))
    due, reads = asyncio.run(probe.read_parser_records(repo, scope))
    assert "item-unstamped" not in {r.item_id for r in due}
    assert reads == [(NOW, 1)]                          # only the reader record; the unstamped one is not counted
    drive = _FakeDrive(blobs)
    conn = SimpleNamespace(scope=scope, handle=SimpleNamespace())
    _, counts, run_reads, failed = asyncio.run(probe.run(
        [(conn, "u1")], repo=repo, drive=drive, extractor=_FakeExtractor(outcomes), space=SPACE, count=_count,
        sample=5000, log=lambda _: None))
    assert failed == 0 and drive.downloads == 3 and run_reads == [(NOW, 1)]
    assert counts == {"pdf": (2, 2), "docx": (1, 1)}


def test_the_reader_ledger_reads_the_last_months(probe):
    class Repo:
        def __init__(self):
            self.asked = []

        async def get_model_usage(self, user_id, month):
            self.asked.append(month)
            return ModelUsageMonth(reader_usd=0.5 if month == "2026-10" else 0.0)

    repo = Repo()
    out = asyncio.run(probe.reader_ledger(repo, "u1", NOW))
    assert repo.asked[:3] == ["2026-11", "2026-10", "2026-09"] and out["2026-10"] == 0.5 and len(out) == probe.USAGE_MONTHS
