"""
Generate the Search v2 eval overlay's files (tests/fixtures/drive_eval/files_v2/, named by tree_v2.json;
USER_DRIVE_INDEXING_RFC §25.7). Committed, like files/: the recorded enrichment snapshot (enrichment.json) is keyed by
these bytes and their text, so regenerating is a deliberate fixture change followed by a re-record.

    venv/bin/python -m scripts.validation.drive_eval.make_fixtures_v2

Every format here is byte-deterministic (one-page PDFs written byte for byte by mini_pdf, DOCX with fixed core
properties and zip entry times, UTF-8 text): no Chromium, so any machine makes the same bytes. The §26 pictures are
drawn with Pillow (fixture_images_s26.py): the same bytes for the same Pillow version. Words in fixture_texts_v2.py.
"""
from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Sequence

from . import fixture_images_s26 as IMG
from . import fixture_texts_s26 as S26
from . import fixture_texts_v2 as T
from .make_fixtures import FIXED_TIME, docx_bytes, text_bytes
from .mini_pdf import text_pdf


def fixed_docx(paragraphs: Sequence[str]) -> bytes:
    """python-docx stamps its zip entries with the current time: re-zip them with FIXED_TIME, so the same words give
    the same bytes on any day."""
    source = zipfile.ZipFile(io.BytesIO(docx_bytes(paragraphs)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for info in source.infolist():
            fixed = zipfile.ZipInfo(info.filename, FIXED_TIME)
            fixed.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(fixed, source.read(info.filename))
    return out.getvalue()


OUT = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "drive_eval" / "files_v2"

FIXTURES_V2: Dict[str, Callable[[], bytes]] = {
    "lab_report_2025_03.pdf": lambda: text_pdf(T.LAB_REPORT_ES, font_size=10),
    "receipt_supermarket_es.pdf": lambda: text_pdf(T.RECEIPT_SUPERMARKET_ES),
    "receipt_hardware_en.pdf": lambda: text_pdf(T.RECEIPT_HARDWARE_EN),
    "receipt_pharmacy_ru.txt": lambda: text_bytes(T.RECEIPT_PHARMACY_RU),
    "motor_policy_en.pdf": lambda: text_pdf(T.MOTOR_POLICY_EN),
    "vehicle_inspection_es.pdf": lambda: text_pdf(T.VEHICLE_INSPECTION_ES),
    "warranty_washer_es.pdf": lambda: text_pdf(T.WARRANTY_WASHER_ES),
    "gym_pass_uk.md": lambda: text_bytes(T.GYM_PASS_UK),
    "invoice_bathroom_es.pdf": lambda: text_pdf(T.INVOICE_BATHROOM_ES, font_size=10),
    "invoice_painting_es.pdf": lambda: text_pdf(T.INVOICE_PAINTING_ES),
    "hotel_booking_ru.docx": lambda: fixed_docx(T.HOTEL_BOOKING_RU),
    "weekend_ideas.md": lambda: text_bytes(T.WEEKEND_IDEAS_EN),
    "trip_notes_uk.md": lambda: text_bytes(T.TRIP_NOTES_UK),
    "flat_viewing_uk.md": lambda: text_bytes(T.FLAT_VIEWING_UK),
    "house_hunting.md": lambda: text_bytes(T.HOUSE_HUNTING_EN),
    "history_of_bridges.docx": lambda: fixed_docx(T.book_paragraphs()),
    # §26.8 Eval, D39, D40 (fixture_texts_s26.py): drawn with Pillow, so these pin its encoders (fixture_images_s26.py)
    "id_card_scan.pdf": lambda: IMG.id_card_scan_pdf(S26.ID_CARD_LINES),
    "meter_card_photo.jpg": lambda: IMG.document_photo_jpeg(S26.METER_CARD_LINES),
    "loan_screenshot.png": lambda: IMG.screenshot_png(S26.LOAN_SCREEN_LINES),
    "chimney_certificate.pdf": lambda: text_pdf(S26.CHIMNEY_CERT_LINES),
    "photo_sunset.jpg": IMG.sunset_jpeg,
    "photo_beach.jpg": IMG.beach_jpeg,
    "photo_landmark.jpg": IMG.landmark_jpeg,
    # the Hamburg and Coimbra photos (tree_v2.json): pictures of their own, so no recorded triage answer crosses places
    "photo_harbour.jpg": IMG.harbour_jpeg,
    "photo_riverside.jpg": IMG.riverside_jpeg,
}


def write_fixtures_v2(out: Path = OUT, only: Sequence[str] = ()) -> List[Path]:
    unknown = sorted(set(only) - set(FIXTURES_V2))
    if unknown:
        raise SystemExit(f"unknown fixture(s): {', '.join(unknown)}")
    out.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for name, build in FIXTURES_V2.items():
        if only and name not in only:
            continue
        path = out / name
        path.write_bytes(build())
        written.append(path)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.make_fixtures_v2")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--only", action="append", default=[], help="one file name; repeatable")
    args = parser.parse_args(argv)
    for path in write_fixtures_v2(args.out, args.only):
        sys.stdout.write(f"{path.stat().st_size:>8}  {path.name}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
