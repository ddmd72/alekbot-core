"""The keyer is versioned (USER_DRIVE_INDEXING_RFC §25.2.4, §25.4.8). Two pins per KEYS_VERSION: a hash of every table
an index key depends on, and a hash of the keyer's output on a fixed multilingual fixture (P1 review m9: a change to
a rule's code, not only to a table). Either failing means: bump KEYS_VERSION (and NAME_KEYS_V2 when name keys change),
then pin the new digests beside the old ones."""
import hashlib

from src.domain.drive_index import KEYS_VERSION, TriageVerdict
from src.domain.drive_key_sources import RecordKeyInputs
from src.domain.drive_keys import NAME_KEYS_V2, about_keys_of, golden_digest, index_tables_digest, query_concepts

TABLES = {
    (1, 2): "c54ed0c7d2c1c0a8601c71047dac8663c88bb301f55798373feb7ae7f836d3f4",
    (2, 2): "35c2d15703fcf1875fbf7f065fe9a735cd3d2696484d90a9bad048835a8e1140",
    # KEYS_VERSION 3 (xc F1 `ef:`, F3 `xl:`/`exl:`, D31 fixed label names, numbers/interrogatives)
    (3, 2): "f41fa1f4bb1f15a4138ccb08c2e5d405a6ea705e7e50a42f12574ebe0d28ab3a",
    # KEYS_VERSION 4 (§26.7), S2 so far: CAPTURE_NAMES and the `cp:`/`cpa:` families. Unreleased: S3's keys move it
    (4, 2): "6df876eccb1e8bce5770c8e3e2d48b8a503ba123189440158d8077ac678422d8",
}
GOLDEN = {
    (2, 2): "eaddca63bfa2498893cb5644b06e065234c73eab151ec80eace891e3a04a0af5",
    (3, 2): "7c3482296ff0118b7e3e67b5a22e33c6dca74636f2bdd7af68caf2d1484e5341",   # KEYS_VERSION 3, as above
    # KEYS_VERSION 4, as above: equal to (3, 2) — the fixture holds no capture record, and a query word emits `cp:`/
    # `cpa:` only for a form a capture name is stored under (none in these texts); CAPTURE_GOLDEN below covers them
    (4, 2): "7c3482296ff0118b7e3e67b5a22e33c6dca74636f2bdd7af68caf2d1484e5341",
}
# The capture keys (§26.3, Δ57, Δ58) on their own fixture, pinned per KEYS_VERSION like GOLDEN: a detected scan, a
# camera-detected document photo, a model's screenshot, a digital file (no words), a photograph (no capture keys
# whatever it holds), and queries holding capture words in four languages.
CAPTURE_GOLDEN = {
    (4, 2): "31226bfc3a182be45f5eb0f0a9fc9b8b6d15f1ba722dca66ab1dbf06b3094d1b",
}


def test_the_index_tables_are_pinned_to_their_versions():
    digest = index_tables_digest()
    assert TABLES.get((KEYS_VERSION, NAME_KEYS_V2)) == digest, (
        f"keyer tables changed (digest {digest}): bump KEYS_VERSION / NAME_KEYS_V2 and pin the new digest")


def test_the_keyer_output_is_pinned_to_its_versions():
    digest = golden_digest()
    assert GOLDEN.get((KEYS_VERSION, NAME_KEYS_V2)) == digest, (
        f"keyer output changed (digest {digest}): bump KEYS_VERSION / NAME_KEYS_V2 and pin the new digest")


def _capture_digest() -> str:
    recs = [RecordKeyInputs(kind="pdf", capture="scan", capture_source="detected"),
            RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.TEXT, capture="document_photo",
                            capture_source="detected"),
            RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.TEXT, capture="screenshot",
                            capture_source="automatic"),
            RecordKeyInputs(kind="docx", capture="digital", capture_source="detected"),
            RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO, capture="scan", capture_source="automatic")]
    queries = ("скани паспорта", "скан-копія договору", "document photos", "знімки екрана", "escaneados", "screen shot")
    out = [about_keys_of(r) for r in recs] + [query_concepts(q) for q in queries]
    return hashlib.sha256(repr(out).encode("utf-8")).hexdigest()


def test_the_capture_keyer_output_is_pinned_to_its_versions():
    digest = _capture_digest()
    assert CAPTURE_GOLDEN.get((KEYS_VERSION, NAME_KEYS_V2)) == digest, (
        f"capture keyer output changed (digest {digest}): bump KEYS_VERSION / NAME_KEYS_V2 and pin the new digest")


def test_the_digests_are_stable_across_calls():
    assert index_tables_digest() == index_tables_digest() and golden_digest() == golden_digest()
    assert _capture_digest() == _capture_digest()
