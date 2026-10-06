from pathlib import Path
import pytest
from src.adapters.filesystem_skill_loader import load_system_skill_bundle, load_system_skills
from src.domain.exceptions import SkillValidationError
from src.domain.skill import sha256_text

SKILL = '---\nname: {n}\ndescription: "Use when x."\n---\nbody\n'


def _skill(root: Path, name: str, files: dict) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL.format(n=name), encoding="utf-8")
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def test_bundle_builds_manifest_and_contents(tmp_path):
    _skill(tmp_path, "s", {"references/a.md": "AAA", "data.csv": "x,y"})
    skills, contents = load_system_skill_bundle(tmp_path)
    s = skills[0]
    assert [f.path for f in s.files] == ["data.csv", "references/a.md"]
    assert contents["s"][sha256_text("AAA")] == "AAA"
    assert load_system_skills(tmp_path)[0].files == s.files


@pytest.mark.parametrize("rel,content", [
    ("image.png", "x"), ("references/тест.md", "x"), ("empty.md", ""),
    ("big.md", "x" * (256 * 1024 + 1)),
])
def test_bad_file_fails_startup(tmp_path, rel, content):
    _skill(tmp_path, "s", {rel: content})
    with pytest.raises(SkillValidationError):
        load_system_skill_bundle(tmp_path)


def test_non_utf8_fails_startup(tmp_path):
    _skill(tmp_path, "s", {})
    (tmp_path / "s" / "bad.txt").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(SkillValidationError):
        load_system_skill_bundle(tmp_path)


def test_dotfiles_are_ignored(tmp_path):
    _skill(tmp_path, "s", {"references/a.md": "x"})
    (tmp_path / "s" / ".DS_Store").write_bytes(b"\x00\x01")
    (tmp_path / "s" / "references" / ".gitkeep").write_text("")
    skills, _ = load_system_skill_bundle(tmp_path)
    assert [f.path for f in skills[0].files] == ["references/a.md"]


def test_symlink_fails_startup(tmp_path):
    _skill(tmp_path, "s", {"real.md": "x"})
    (tmp_path / "s" / "link.md").symlink_to(tmp_path / "s" / "real.md")
    with pytest.raises(SkillValidationError):
        load_system_skill_bundle(tmp_path)
