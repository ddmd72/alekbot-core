"""`_native_file` leaves no temp file behind when writing it fails."""
import pytest

from src.services import file_conversion_service as fcs
from src.domain.document_read import ReadOutcome, ReadStatus


def test_a_failed_write_removes_the_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fcs.tempfile, "tempdir", str(tmp_path))
    real_fdopen = fcs.os.fdopen

    def failing_fdopen(fd, mode):
        f = real_fdopen(fd, mode)

        class Broken:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                f.close()

            def write(self, data):
                raise OSError("disk full")

        return Broken()

    monkeypatch.setattr(fcs.os, "fdopen", failing_fdopen)
    outcome = ReadOutcome(status=ReadStatus.NATIVE, native_mime="application/pdf")
    with pytest.raises(OSError):
        fcs._native_file(outcome, b"%PDF", "application/pdf", "a.pdf")
    assert list(tmp_path.iterdir()) == []


def test_a_written_native_file_exists_for_its_consumer(tmp_path, monkeypatch):
    monkeypatch.setattr(fcs.tempfile, "tempdir", str(tmp_path))
    outcome = ReadOutcome(status=ReadStatus.NATIVE, native_mime="application/pdf")
    opened = fcs._native_file(outcome, b"%PDF", "application/pdf", "a.pdf")
    assert [p.name for p in tmp_path.iterdir()] == [opened.native_path.rsplit("/", 1)[1]]
