"""A small manifest and gold set in the eval's schema (§17.8), for the harness tests."""

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
LEASE_HASH = {"file": {"hashes": {"quickXorHash": "bGVhc2U="}}}
MANIFEST = {
    "items": [
        {"path": "Contracts", "folder": True},
        {"path": "Contracts/Договор_ред1.docx", "folder": False, "mime": DOCX, "size": 2048,
         "fs_modified": "2026-03-01T10:00:00Z", "facets": {}},
        {"path": "Contracts/Lease agreement — Porto.pdf", "folder": False, "mime": "application/pdf",
         "size": 4096, "fs_modified": "2026-02-01T10:00:00Z", "facets": LEASE_HASH},
        {"path": "Inbox/Lease agreement — Porto.pdf", "folder": False, "mime": "application/pdf",
         "size": 4096, "fs_modified": "2026-02-02T10:00:00Z", "facets": LEASE_HASH},
        {"path": "Scans/Scan_0012.pdf", "folder": False, "mime": "application/pdf", "size": 1000,
         "fs_modified": "2026-04-01T10:00:00Z", "facets": {}},
        {"path": "Photos/Rome 2025/IMG_0001.HEIC", "folder": False, "mime": "image/heic", "size": 3000000,
         "fs_modified": "2025-10-01T10:00:00Z",
         "facets": {"photo": {"takenDateTime": "2025-08-14T18:00:00Z", "cameraMake": "Apple",
                              "cameraModel": "iPhone 15"},
                    "location": {"latitude": 41.9, "longitude": 12.49}, "image": {"width": 4032, "height": 3024}}},
        {"path": "Projects/alek-site/.git", "folder": True},
        {"path": "Projects/alek-site/src/main.py", "folder": False, "mime": "text/x-python", "size": 100,
         "fs_modified": "2026-05-01T10:00:00Z", "facets": {}},
    ],
    "generate": [
        {"folder": "Dumps/site-mirror", "pattern": "page-{n:03d}.bin", "count": 520,
         "mime": "application/octet-stream", "size": 10, "facets_template": {}},
        {"folder": "Vault", "pattern": "note-{n:03d}.md", "count": 3, "mime": "text/markdown", "size": 10,
         "facets_template": {"file": {"hashes": {"quickXorHash": "note{n}="}}}},
    ],
}
GOLD = [
    {"id": "ex1", "query": "Scan_0012", "lang": "en", "goal": "G1", "filters": {},
     "expected_paths": ["Scans/Scan_0012.pdf"], "case": "exact"},
    {"id": "tr1", "query": "dogovor red1", "lang": "ru", "goal": "G1", "filters": {},
     "expected_paths": ["Contracts/Договор_ред1.docx"], "case": "translit"},
    {"id": "fo1", "query": "lease", "lang": "en", "goal": "G1", "filters": {"folder": "Contracts"},
     "expected_paths": ["Contracts/Lease agreement — Porto.pdf"], "case": "folder"},
    {"id": "dt1", "query": "", "lang": "en", "goal": "G1",
     "filters": {"date_from": "2025-08-01", "date_to": "2025-08-31"},
     "expected_paths": ["Photos/Rome 2025/IMG_0001.HEIC"], "case": "date"},
]


def rome(lat: float, lon: float) -> str:
    return "Rome (Roma), Italy"


def fake_content():
    """The content ports as the eval CLI wires the real ones since the flip (§24.1), faked: a parser, a reader and
    image triage, as keyword arguments of index_fixture / run_eval / live_check."""
    from src.domain.drive_extraction import PARSER, READER

    from ..services.drive_content_fakes import FakeExtractor, FakeTriage
    return {"extractors": {PARSER: FakeExtractor(PARSER), READER: FakeExtractor(READER)}, "triage": FakeTriage()}
