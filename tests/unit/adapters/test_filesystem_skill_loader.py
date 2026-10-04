import pytest

from src.adapters.filesystem_skill_loader import SYSTEM_SKILLS_ROOT, load_system_skills
from src.domain.exceptions import SkillValidationError


def _write(root, folder, text):
    d = root / folder
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")


def test_loads_sorted(tmp_path):
    _write(tmp_path, "b-one", "---\nname: b-one\ndescription: Use when b.\n---\nbody b\n")
    _write(tmp_path, "a-one", "---\nname: a-one\ndescription: Use when a.\n---\nbody a\n")
    assert [s.name for s in load_system_skills(tmp_path)] == ["a-one", "b-one"]


def test_folder_name_must_match(tmp_path):
    _write(tmp_path, "folder", "---\nname: other\ndescription: Use when x.\n---\nb\n")
    with pytest.raises(SkillValidationError):
        load_system_skills(tmp_path)


def test_malformed_fails(tmp_path):
    _write(tmp_path, "x", "no frontmatter")
    with pytest.raises(SkillValidationError):
        load_system_skills(tmp_path)


def test_ignores_non_directories(tmp_path):
    (tmp_path / "README.md").write_text("x")
    assert load_system_skills(tmp_path) == []


def test_root_missing_raises(tmp_path):
    with pytest.raises(SkillValidationError):
        load_system_skills(tmp_path / "does-not-exist")


def test_root_not_a_directory_raises(tmp_path):
    f = tmp_path / "not-a-dir"
    f.write_text("x")
    with pytest.raises(SkillValidationError):
        load_system_skills(f)


def test_subfolder_without_skill_md_raises(tmp_path):
    (tmp_path / "empty-folder").mkdir()
    with pytest.raises(SkillValidationError):
        load_system_skills(tmp_path)


def test_nothing_loaded_raises(tmp_path):
    # Root exists and is a directory, but holds only a plain file — zero skills.
    (tmp_path / "README.md").write_text("x")
    with pytest.raises(SkillValidationError):
        load_system_skills(tmp_path)


def test_every_repo_system_skill_parses():
    names = {s.name for s in load_system_skills(SYSTEM_SKILLS_ROOT)}
    assert {"skill-creator", "domain-competency-research"} <= names
