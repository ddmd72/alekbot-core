import pytest
from pydantic import ValidationError

from src.domain.skill import (
    MAX_FILES_PER_SKILL, MAX_SKILL_FILE_BYTES, FileChangeLine, Skill, SkillFile, SkillFileChange,
    change_summary, is_delivered_ref, merge_manifest, merge_visible_skills, parse_skill_ref,
    render_files_block, sha256_text, skill_file_ref, validate_file_path,
)


def _f(path, content="x"):
    return SkillFile(path=path, sha256=sha256_text(content), size=len(content.encode()))


@pytest.mark.parametrize("path", ["references/a.md", "a.txt", "refs/sub/data.csv", "x.yml", "v1.2-notes.json"])
def test_valid_paths(path):
    assert validate_file_path(path) == path


@pytest.mark.parametrize("path", [
    "", "/abs.md", "../up.md", "refs/../x.md", "./a.md", "refs//a.md", "SKILL.md",
    "noext", "image.png", "doc.pdf", "references/тарифы.md", "my notes.md", "a" * 198 + ".md",
])
def test_invalid_paths_raise_with_reason(path):
    with pytest.raises(ValueError) as e:
        validate_file_path(path)
    assert str(e.value)  # a reason the model can act on


def test_cyrillic_rejection_names_allowed_characters():
    with pytest.raises(ValueError, match=r"A-Za-z0-9"):
        validate_file_path("references/тарифы.md")


def test_sha256_is_hex_of_utf8():
    import hashlib
    assert sha256_text("é") == hashlib.sha256("é".encode("utf-8")).hexdigest()


def test_skill_file_bounds():
    with pytest.raises(ValidationError):
        SkillFile(path="a.md", sha256="nothex", size=1)
    with pytest.raises(ValidationError):
        SkillFile(path="a.md", sha256="a" * 64, size=MAX_SKILL_FILE_BYTES + 1)
    with pytest.raises(ValidationError):
        SkillFile(path="a.md", sha256="a" * 64, size=0)


def test_skill_files_cap_and_unique_paths():
    with pytest.raises(ValidationError):
        Skill(name="s", description="Use when x.", body="b",
              files=[_f(f"r{i}.md", str(i)) for i in range(MAX_FILES_PER_SKILL + 1)])
    with pytest.raises(ValidationError):
        Skill(name="s", description="Use when x.", body="b", files=[_f("a.md", "1"), _f("a.md", "2")])


def test_change_needs_exactly_one_action():
    SkillFileChange(path="a.md", content="x")
    SkillFileChange(path="a.md", from_file="up.md")
    SkillFileChange(path="a.md", remove=True)
    for bad in ({}, {"content": "x", "remove": True}, {"content": "x", "from_file": "u.md"}):
        with pytest.raises(ValidationError):
            SkillFileChange(path="a.md", **bad)


def test_refs_roundtrip_and_delivered():
    ref = skill_file_ref("flight-status", "references/airlines.md")
    assert ref == "skill:flight-status/references/airlines.md"
    assert parse_skill_ref(ref) == ("flight-status", "references/airlines.md")
    assert parse_skill_ref("report.docx") is None
    assert parse_skill_ref("skill:") is None
    assert parse_skill_ref("skill:noslash") is None
    assert is_delivered_ref("email_review/u1/x.html") and not is_delivered_ref("rates.csv")


def test_merge_manifest_inherits_replaces_removes_adds():
    current = [_f("a.md", "A"), _f("b.md", "B"), _f("c.md", "C")]
    new = merge_manifest(current, [("b.md", "B2"), ("c.md", None), ("d.md", "D")])
    by = {f.path: f for f in new}
    assert by["a.md"] == current[0]                 # inherited untouched
    assert by["b.md"].sha256 == sha256_text("B2")   # replaced
    assert "c.md" not in by                         # removed
    assert by["d.md"].sha256 == sha256_text("D")    # added
    assert [f.path for f in new] == sorted(by)      # deterministic order


def test_merge_manifest_rejects_duplicate_paths_and_unknown_remove():
    with pytest.raises(ValueError, match="duplicate"):
        merge_manifest([], [("a.md", "1"), ("a.md", None)])
    with pytest.raises(ValueError, match="not in the skill"):
        merge_manifest([], [("ghost.md", None)])


def test_change_summary_lines():
    current = [_f("a.md", "A"), _f("b.md", "B"), _f("c.md", "C"), _f("u.md", "U")]
    new = merge_manifest(current, [("b.md", "B2"), ("c.md", None), ("d.md", "DD"), ("e.csv", "E")])
    lines = change_summary(current, new, sources={"e.csv": "rates (2).csv"})
    assert lines == [
        FileChangeLine(kind="changed", path="b.md", size=2),
        FileChangeLine(kind="new", path="d.md", size=2),
        FileChangeLine(kind="new", path="e.csv", size=1, source="rates (2).csv"),
        FileChangeLine(kind="removed", path="c.md"),
        FileChangeLine(kind="unchanged", count=2),
    ]


def test_change_summary_empty_when_no_files_anywhere():
    assert change_summary([], [], {}) == []


def test_render_files_block():
    s = Skill(name="fs", description="Use when x.", body="b", files=[_f("references/a.md", "hello")])
    block = render_files_block(s)
    assert block.startswith("Files")
    assert "skill:fs/references/a.md" in block
    assert render_files_block(Skill(name="fs", description="Use when x.", body="b")) == ""


@pytest.mark.parametrize("path", ["refs\n/a.md", "a.md\n", "refs/a.md\n"])
def test_newline_in_path_rejected(path):
    with pytest.raises(ValueError):
        validate_file_path(path)


def test_skill_ref_with_invalid_name_is_not_a_ref():
    assert parse_skill_ref("skill:__x__/a.md") is None
    assert parse_skill_ref("skill:Bad Name/a.md") is None


def test_manifest_budget_fits_firestore_document():
    # 20 entries with maximum-length paths next to a maximum-size SKILL.md stay under 1 MiB (RFC §15.2).
    from src.domain.skill import MAX_SKILL_MD_BYTES
    import json
    files = [SkillFile(path=f"{i:02d}" + "a" * 194 + ".md", sha256="a" * 64, size=MAX_SKILL_FILE_BYTES)
             for i in range(MAX_FILES_PER_SKILL)]
    manifest_bytes = len(json.dumps([f.model_dump() for f in files]).encode())
    assert MAX_SKILL_MD_BYTES + manifest_bytes + 4096 < 1024 * 1024


def test_merge_visible_custom_shadows_system():
    sys_s = Skill(name="a", description="Use when sys.", body="sys")
    cus = Skill(name="a", description="Use when cus.", body="cus", version=2)
    other = Skill(name="b", description="Use when b.", body="b", version=1)
    merged = merge_visible_skills({"a": sys_s}, [cus, other])
    assert merged["a"].body == "cus" and set(merged) == {"a", "b"}


# --- G3: model-shaped entries are coerced, empty content is not ----------------------------

def test_file_change_null_remove_is_false():
    c = SkillFileChange.model_validate({"path": "a.md", "content": "x", "remove": None})
    assert c.remove is False and c.content == "x"


@pytest.mark.parametrize("empty", ["", None])
def test_file_change_empty_from_file_is_none(empty):
    c = SkillFileChange.model_validate({"path": "a.md", "content": "x", "from_file": empty})
    assert c.from_file is None and c.content == "x"


def test_file_change_empty_content_is_not_coerced():
    c = SkillFileChange.model_validate({"path": "a.md", "content": ""})
    assert c.content == ""
    with pytest.raises(ValidationError):
        SkillFileChange.model_validate({"path": "a.md", "content": "", "from_file": "up.md"})
