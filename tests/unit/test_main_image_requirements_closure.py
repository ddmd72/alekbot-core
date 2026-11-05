"""The main image's requirements.txt covers every third-party import its entry points can reach."""
from tests.unit.main_image_closure import (
    NOT_EXECUTED, main_image_missing, missing_distributions, needed_top_levels, requirement_names,
)

# Parsing libraries that must stay out of the main image: they run in the sandbox (requirements-sandbox*.txt).
_PARSERS = {"pypdf", "pdfminer", "pdfplumber", "xlrd", "olefile", "striprtf", "pptx", "mammoth", "magika",
            "defusedxml", "PIL", "markdownify"}


def test_every_distribution_the_entry_points_import_is_installed_by_requirements_txt():
    assert main_image_missing() == {}


def test_the_closure_reaches_no_parsing_library():
    assert needed_top_levels() & _PARSERS == set()


def test_not_executed_entries_name_their_chain():
    assert all("src/" in why for why in NOT_EXECUTED.values())


def test_a_distribution_nothing_provides_is_reported():
    got = missing_distributions({"fastpdf", "yaml", "google"}, {"pyyaml", "google-auth"},
                                {"fastpdf": ["fastpdf"], "yaml": ["PyYAML"], "google": ["google-auth", "protobuf"]})
    assert got == {"fastpdf": ["fastpdf"]}


def test_requirement_lines_are_parsed_to_normalised_names():
    assert requirement_names("Pillow==13.0.0  # x\n# c\npython_docx>=1.1\n-r other.txt\nmcp==1.27.*\n") == {
        "pillow", "python-docx", "mcp"}
