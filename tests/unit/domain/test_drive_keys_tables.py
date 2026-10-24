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
    # KEYS_VERSION 4 (§26.7, unreleased): CAPTURE_NAMES and the `cp:`/`cpa:` families (S2), the `va:`/`ya:`/`ma:`
    # families (S3, Δ60), SCENE_NAMES, PHOTO_SCENE_LABELS_MAX and the `sl:` family (S6, D40), and the spaced-date
    # pattern (Δ62) now in the digest (without it: cc15faff…)
    (4, 2): "ec48414c73a7595201b6b86aa5bca87c9d3fe124a7a49ceb8fda7ef57989a23b",
}
GOLDEN = {
    (2, 2): "eaddca63bfa2498893cb5644b06e065234c73eab151ec80eace891e3a04a0af5",
    (3, 2): "7c3482296ff0118b7e3e67b5a22e33c6dca74636f2bdd7af68caf2d1484e5341",   # KEYS_VERSION 3, as above
    # KEYS_VERSION 4, as above: (3, 2) plus the query's `va:`/`ma:` beside `v:`/`m:` (Δ60) and `sl:` for the scene
    # names in these texts ("квартиры", "houses"; D40). The fixture holds no capture record and no capture word;
    # CAPTURE_GOLDEN below covers them. Re-pinned when a lease text's city became an invented one (2026-10-24): the
    # older pins above describe the texts before that change
    (4, 2): "c985a4034ed86ede8900cb8a5f3da490eb23aa0ccacfde1500fca6c320784944",
}
# The capture keys (§26.3, Δ57, Δ58) on their own fixture, pinned per KEYS_VERSION like GOLDEN: a detected scan, a
# camera-detected document photo, a model's screenshot, a digital file (no words), a photograph (no capture keys
# whatever it holds), and queries holding capture words in four languages.
CAPTURE_GOLDEN = {
    # with the queries' `va:` (Δ60); stripped of it, the S2 digest 31226bfc… again
    (4, 2): "33ca033c785a57620147ef19d558701c98ba0fb09d707620c9e04b8d02745f15",
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
