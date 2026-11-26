"""The names, keys and shown sizes of a task's files (EXTERNAL_TASK_QUEUE_RFC §5.12): a name from any source becomes
one safe storage-key segment that the owner and the worker both see."""
import pytest

from src.domain.external_task import TASK_FILE_NAME_MAX_CHARS
from src.domain.external_task_files import size_label, task_file_key, task_file_name


@pytest.mark.parametrize("raw,name", [
    ("Tom's plan.docx", "Tom's plan.docx"),                     # an ordinary name is kept as it is
    ("R&D/Tom's plan.docx", "Tom's plan.docx"),                 # a path keeps its last segment
    ("C:\\R&D\\birth-certificate.pdf", "birth-certificate.pdf"),
    ("birth-certificate\n.pdf", "birth-certificate_.pdf"),      # control characters
    ("birth\u202ecertificate.pdf", "birth_certificate.pdf"),    # a bidirectional override
    ("birth\u00a0certificate.pdf", "birth_certificate.pdf"),    # a separator other than the space
    ("Tom's plan#1?.docx", "Tom's plan_1_.docx"),               # characters a key or a ref reserves
    ("..birth-certificate.pdf", "birth-certificate.pdf"),       # no leading dot
    ("", "file"), ("/", "file"), ("...", "file"),               # never empty
])
def test_a_name_from_any_source_becomes_one_safe_segment(raw, name):
    assert task_file_name(raw) == name


def test_a_decomposed_name_is_stored_composed():
    assert task_file_name("Tom's pla\u0308n.docx") == "Tom's pl\u00e4n.docx"   # a client sends NFD; the key keeps NFC


def test_a_long_name_is_cut_with_its_extension_kept():
    name = task_file_name("Tom's plan " * 30 + ".docx")
    assert len(name) == TASK_FILE_NAME_MAX_CHARS and name.endswith(".docx")
    assert len(task_file_name("Tom's plan" * 30)) == TASK_FILE_NAME_MAX_CHARS


def test_the_key_names_the_user_the_task_and_the_numbered_file():
    assert task_file_key("usr-1", "tsk-1", 2, "birth-certificate.pdf") == \
        "external_tasks/usr-1/tsk-1/2-birth-certificate.pdf"


@pytest.mark.parametrize("size,label", [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (1536, "1.5 KB"),
                                        (1024 * 1024, "1.0 MB"), (45 * 1024 * 1024, "45.0 MB")])
def test_sizes_read_in_binary_units(size, label):
    assert size_label(size) == label
