"""SkillService with text files in a skill (delivery C, RFC §15.5, §15.7)."""

from unittest.mock import AsyncMock

import pytest

from src.domain.exceptions import SkillFileMissing, SkillRejected
from src.domain.prompt_v3.security import RiskLevel, TrustZone, ValidationResult
from src.domain.skill import (
    MAX_CUSTOM_SKILLS_PER_USER,
    MAX_SKILL_FILE_BYTES,
    FileChangeLine,
    Skill,
    SkillFile,
    SkillFileChange,
    sha256_text,
)
from src.ports.security_port import SecurityPort
from src.ports.skill_repository import SkillRepository
from src.services.file_conversion_service import FileConversionService
from src.services.skill_service import SkillService


def _result(text, action="passed"):
    return ValidationResult(
        sanitized_text=text,
        risk_level=RiskLevel.SAFE if action == "passed" else RiskLevel.HIGH,
        risk_score=0.0,
        patterns_detected=[] if action == "passed" else ["ignore_previous"],
        action_taken=action,
        metadata={},
    )


def _file(path, content):
    return SkillFile(path=path, sha256=sha256_text(content), size=len(content.encode("utf-8")))


SYSTEM_SKILL = Skill(name="skill-creator", description="System skill.", body="System body.")
NEW = Skill(name="fs", description="Use when x.", body="b")


@pytest.fixture
def repo():
    r = AsyncMock(spec=SkillRepository)
    r.get_current.return_value = None
    r.list_current.return_value = []
    r.create_draft.return_value = True
    r.save_version.return_value = 2
    return r


@pytest.fixture
def security():
    s = AsyncMock(spec=SecurityPort)
    s.validate.side_effect = lambda text, context, zone=TrustZone.UNTRUSTED: _result(text)
    return s


@pytest.fixture
def conversion():
    return AsyncMock(spec=FileConversionService)


@pytest.fixture
def svc(repo, security, conversion):
    return SkillService(repo, security, system_skills=[SYSTEM_SKILL], file_conversion=conversion)


# ---------------------------------------------------------------------------
# draft — model-written files
# ---------------------------------------------------------------------------

async def test_draft_with_model_file_stages_content_and_summarizes(svc, repo):
    repo.get_current.return_value = None
    repo.list_current.return_value = []
    repo.create_draft.return_value = True
    res = await svc.draft("u1", Skill(name="fs", description="Use when x.", body="b"),
                          [SkillFileChange(path="references/a.md", content="AAA")])
    staged = repo.create_draft.call_args.kwargs["staged"]
    assert staged == {sha256_text("AAA"): "AAA"}
    assert res.skill.files[0].path == "references/a.md"
    assert res.model_files == [("references/a.md", "AAA")]
    assert res.summary == [FileChangeLine(kind="new", path="references/a.md", size=3)]
    assert res.code == repo.create_draft.call_args.args[1]


async def test_draft_reads_list_current_once_and_never_get_current(svc, repo):
    await svc.draft("u1", NEW, [SkillFileChange(path="a.md", content="AAA")])

    repo.list_current.assert_awaited_once_with("u1")
    repo.get_current.assert_not_awaited()


async def test_body_only_revision_inherits_files_and_stages_nothing(svc, repo):
    a = _file("a.md", "AAA")
    b = _file("data/b.csv", "x,y\n")
    repo.list_current.return_value = [Skill(name="fs", description="Use when x.", body="old", version=1, files=[a, b])]

    res = await svc.draft("u1", Skill(name="fs", description="Use when x.", body="new body"))

    assert res.skill.files == [a, b]
    assert res.skill.body == "new body"
    assert repo.create_draft.call_args.kwargs["staged"] == {}
    assert res.model_files == []
    assert res.summary == [FileChangeLine(kind="unchanged", count=2)]
    created_skill = repo.create_draft.call_args.args[2]
    assert created_skill.files == [a, b]


async def test_files_only_change_is_valid_draft(svc, repo):
    a = _file("a.md", "AAA")
    current = Skill(name="fs", description="Use when x.", body="same", version=3, files=[a])
    repo.list_current.return_value = [current]

    res = await svc.draft(
        "u1",
        Skill(name="fs", description="Use when x.", body="same"),
        [SkillFileChange(path="a.md", content="AAA2"), SkillFileChange(path="b.md", content="BBB")],
    )

    assert {f.path for f in res.skill.files} == {"a.md", "b.md"}
    assert repo.create_draft.call_args.kwargs["staged"] == {
        sha256_text("AAA2"): "AAA2", sha256_text("BBB"): "BBB",
    }
    assert res.summary == [
        FileChangeLine(kind="changed", path="a.md", size=4),
        FileChangeLine(kind="new", path="b.md", size=3),
    ]


async def test_unchanged_rewrite_of_existing_content_is_not_staged(svc, repo):
    a = _file("a.md", "AAA")
    repo.list_current.return_value = [Skill(name="fs", description="Use when x.", body="b", version=1, files=[a])]

    await svc.draft("u1", NEW, [SkillFileChange(path="copy.md", content="AAA")])

    assert repo.create_draft.call_args.kwargs["staged"] == {}


async def test_more_than_five_model_files_rejected(svc, repo):
    changes = [SkillFileChange(path=f"f{i}.md", content=f"c{i}") for i in range(6)]

    with pytest.raises(SkillRejected) as e:
        await svc.draft("u1", NEW, changes)

    assert "several drafts" in str(e.value)
    repo.create_draft.assert_not_awaited()


async def test_five_model_files_allowed(svc, repo):
    changes = [SkillFileChange(path=f"f{i}.md", content=f"c{i}") for i in range(5)]

    res = await svc.draft("u1", NEW, changes)

    assert len(res.skill.files) == 5


async def test_draft_new_name_at_cap_with_files_raises(svc, repo):
    from src.domain.exceptions import SkillCapExceeded
    repo.list_current.return_value = [
        Skill(name=f"existing-{i}", description="d", body="b") for i in range(MAX_CUSTOM_SKILLS_PER_USER)
    ]

    with pytest.raises(SkillCapExceeded):
        await svc.draft("u1", NEW, [SkillFileChange(path="a.md", content="AAA")])

    repo.create_draft.assert_not_awaited()


# ---------------------------------------------------------------------------
# draft — from_file
# ---------------------------------------------------------------------------

async def test_from_file_upload_resaved_verbatim_with_source(svc, repo, conversion):
    conversion.resolve_bytes.return_value = b"a,b\n1,2\n"

    res = await svc.draft("u1", NEW, [
        SkillFileChange(path="data/rates.csv", from_file="rates (2).csv"),
        SkillFileChange(path="notes.md", content="N"),
    ])

    conversion.resolve_bytes.assert_awaited_once_with("rates (2).csv", "u1")
    assert repo.create_draft.call_args.kwargs["staged"][sha256_text("a,b\n1,2\n")] == "a,b\n1,2\n"
    assert res.model_files == [("notes.md", "N")]
    assert FileChangeLine(kind="new", path="data/rates.csv", size=8, source="rates (2).csv") in res.summary


async def test_from_file_bom_and_crlf_kept_verbatim(svc, repo, conversion):
    raw = b"\xef\xbb\xbfa,b\r\n1,2\r\n"
    conversion.resolve_bytes.return_value = raw
    text = raw.decode("utf-8")

    res = await svc.draft("u1", NEW, [SkillFileChange(path="r.csv", from_file="r.csv")])

    assert text.startswith("﻿") and "\r\n" in text
    assert repo.create_draft.call_args.kwargs["staged"] == {sha256_text(text): text}
    assert res.skill.files[0].size == len(raw)
    assert res.skill.files[0].sha256 == sha256_text(text)


async def test_from_file_delivered_ref_rejected(svc, repo, conversion):
    with pytest.raises(SkillRejected) as e:
        await svc.draft("u1", NEW, [SkillFileChange(path="r.md", from_file="email_review/u1/x.html")])

    assert "bot-delivered" in str(e.value)
    conversion.resolve_bytes.assert_not_awaited()
    repo.create_draft.assert_not_awaited()


async def test_from_file_non_text_extension_or_invalid_utf8_rejected(svc, repo, conversion):
    with pytest.raises(SkillRejected):
        await svc.draft("u1", NEW, [SkillFileChange(path="r.md", from_file="photo.png")])
    conversion.resolve_bytes.assert_not_awaited()

    conversion.resolve_bytes.return_value = b"\xff\xfe\x00bad"
    with pytest.raises(SkillRejected) as e:
        await svc.draft("u1", NEW, [SkillFileChange(path="r.txt", from_file="r.txt")])
    assert "UTF-8" in str(e.value)
    repo.create_draft.assert_not_awaited()


async def test_from_file_label_not_found_rejected_cleanly(svc, repo, conversion):
    conversion.resolve_bytes.side_effect = FileNotFoundError("nope")

    with pytest.raises(SkillRejected) as e:
        await svc.draft("u1", NEW, [SkillFileChange(path="r.csv", from_file="r (1).csv")])

    assert "not found" in str(e.value)
    repo.create_draft.assert_not_awaited()


async def test_from_file_bare_name_not_found_gives_bare_filename_hint(svc, repo, conversion):
    conversion.resolve_bytes.side_effect = FileNotFoundError("rates.csv")

    with pytest.raises(SkillRejected) as e:
        await svc.draft("u1", NEW, [SkillFileChange(path="r.csv", from_file="rates.csv")])

    assert "pass the bare filename from the file label" in str(e.value)


async def test_from_file_skill_ref_not_found_keeps_resolver_message(svc, repo, conversion):
    other = Skill(name="other", description="Use when y.", body="o", version=1, files=[_file("r.md", "RRR")])
    repo.list_current.return_value = [other]
    msg = "Skill 'other' has no file 'missing.md'. Its files: r.md"
    conversion.resolve_bytes.side_effect = FileNotFoundError(msg)

    with pytest.raises(SkillRejected) as e:
        await svc.draft("u1", NEW, [SkillFileChange(path="x.md", from_file="skill:other/missing.md")])

    assert str(e.value) == msg
    assert "bare filename" not in str(e.value)
    repo.create_draft.assert_not_awaited()


async def test_from_file_invalid_utf8_logs_warning(svc, repo, conversion, caplog):
    conversion.resolve_bytes.return_value = b"\xff\xfebad"

    with caplog.at_level("WARNING"):
        with pytest.raises(SkillRejected):
            await svc.draft("u1", NEW, [SkillFileChange(path="r.txt", from_file="r.txt")])

    assert any("not UTF-8" in r.getMessage() for r in caplog.records)


async def test_from_file_permission_error_rejected_cleanly(svc, repo, conversion):
    conversion.resolve_bytes.side_effect = PermissionError("not yours")

    with pytest.raises(SkillRejected):
        await svc.draft("u1", NEW, [SkillFileChange(path="r.csv", from_file="r.csv")])


async def test_from_file_skill_ref_reads_via_conversion(svc, repo, conversion):
    other = Skill(name="other", description="Use when y.", body="o", version=1, files=[_file("r.md", "RRR")])
    repo.list_current.return_value = [other]
    conversion.resolve_bytes.return_value = b"RRR"

    res = await svc.draft("u1", NEW, [SkillFileChange(path="refs/r.md", from_file="skill:other/r.md")])

    conversion.resolve_bytes.assert_awaited_once_with("skill:other/r.md", "u1")
    assert repo.create_draft.call_args.kwargs["staged"] == {sha256_text("RRR"): "RRR"}
    assert res.summary == [FileChangeLine(kind="new", path="refs/r.md", size=3, source="skill:other/r.md")]


async def test_from_file_system_skill_ref_rejected(svc, repo, conversion):
    repo.list_current.return_value = []

    with pytest.raises(SkillRejected) as e:
        await svc.draft("u1", NEW, [SkillFileChange(path="x.md", from_file="skill:skill-creator/x.md")])

    assert "your own skills" in str(e.value)
    conversion.resolve_bytes.assert_not_awaited()
    repo.create_draft.assert_not_awaited()


async def test_from_file_malformed_skill_ref_rejected(svc, repo, conversion):
    with pytest.raises(SkillRejected):
        await svc.draft("u1", NEW, [SkillFileChange(path="x.md", from_file="skill:Bad_Name")])

    conversion.resolve_bytes.assert_not_awaited()


async def test_from_file_without_file_conversion_rejected(repo, security):
    svc = SkillService(repo, security, system_skills=[SYSTEM_SKILL])

    with pytest.raises(SkillRejected):
        await svc.draft("u1", NEW, [SkillFileChange(path="r.csv", from_file="r.csv")])

    repo.create_draft.assert_not_awaited()


async def test_file_over_256kb_rejected(svc, repo, conversion):
    conversion.resolve_bytes.return_value = b"a" * (MAX_SKILL_FILE_BYTES + 1)
    with pytest.raises(SkillRejected):
        await svc.draft("u1", NEW, [SkillFileChange(path="r.txt", from_file="r.txt")])

    with pytest.raises(SkillRejected):
        await svc.draft("u1", NEW, [SkillFileChange(path="r.txt", content="a" * (MAX_SKILL_FILE_BYTES + 1))])

    repo.create_draft.assert_not_awaited()


async def test_empty_file_rejected(svc, repo):
    with pytest.raises(SkillRejected):
        await svc.draft("u1", NEW, [SkillFileChange(path="r.txt", content="")])

    repo.create_draft.assert_not_awaited()


async def test_flagged_file_content_rejected(svc, repo, security):
    def _validate(text, context, zone=TrustZone.UNTRUSTED):
        return _result(text, "sanitized" if text == "EVIL" else "passed")
    security.validate.side_effect = _validate

    with pytest.raises(SkillRejected) as e:
        await svc.draft("u1", NEW, [SkillFileChange(path="a.md", content="EVIL")])

    assert "a.md" in str(e.value)
    repo.create_draft.assert_not_awaited()
    contexts = [c.kwargs["context"] for c in security.validate.call_args_list]
    assert any("file" in c for c in contexts)


async def test_remove_unknown_path_rejected(svc, repo):
    with pytest.raises(SkillRejected):
        await svc.draft("u1", NEW, [SkillFileChange(path="ghost.md", remove=True)])

    repo.create_draft.assert_not_awaited()


async def test_remove_known_path_summarized(svc, repo):
    a = _file("a.md", "AAA")
    repo.list_current.return_value = [Skill(name="fs", description="Use when x.", body="b", version=1, files=[a])]

    res = await svc.draft("u1", NEW, [SkillFileChange(path="a.md", remove=True)])

    assert res.skill.files == []
    assert res.summary == [FileChangeLine(kind="removed", path="a.md")]


async def test_duplicate_paths_rejected(svc, repo):
    with pytest.raises(SkillRejected):
        await svc.draft("u1", NEW, [
            SkillFileChange(path="a.md", content="1"), SkillFileChange(path="a.md", content="2"),
        ])


# ---------------------------------------------------------------------------
# save_draft
# ---------------------------------------------------------------------------

async def test_save_draft_rechecks_staged_files_and_passes_draft_code(svc, repo, security):
    drafted = Skill(name="fs", description="Use when x.", body="b", files=[_file("a.md", "AAA")])
    repo.get_draft.return_value = drafted
    repo.get_draft_files.return_value = {sha256_text("AAA"): "AAA"}

    result = await svc.save_draft("u1", "a1", "abcd")

    assert result == ("fs", 2)
    repo.get_draft_files.assert_awaited_once_with("u1", "abcd")
    texts = [c.args[0] for c in security.validate.call_args_list]
    assert "AAA" in texts
    repo.save_version.assert_awaited_once_with(
        "u1", "a1", drafted, cap=MAX_CUSTOM_SKILLS_PER_USER, consume_drafts_named="fs", draft_code="abcd",
    )


async def test_save_draft_flagged_staged_file_rejected(svc, repo, security):
    repo.get_draft.return_value = Skill(name="fs", description="Use when x.", body="b", files=[_file("a.md", "EVIL")])
    repo.get_draft_files.return_value = {sha256_text("EVIL"): "EVIL"}
    security.validate.side_effect = lambda text, context, zone=TrustZone.UNTRUSTED: _result(
        text, "sanitized" if text == "EVIL" else "passed")

    with pytest.raises(SkillRejected):
        await svc.save_draft("u1", "a1", "abcd")

    repo.save_version.assert_not_awaited()


async def test_save_draft_corrupted_staged_content_rejected(svc, repo):
    repo.get_draft.return_value = Skill(name="fs", description="Use when x.", body="b", files=[_file("a.md", "AAA")])
    repo.get_draft_files.return_value = {sha256_text("AAA"): "tampered"}

    with pytest.raises(SkillRejected) as e:
        await svc.save_draft("u1", "a1", "abcd")

    assert "corrupted" in str(e.value)
    repo.save_version.assert_not_awaited()


async def test_save_draft_missing_staged_file_from_get_draft_files_rejected(svc, repo):
    repo.get_draft.return_value = Skill(name="fs", description="Use when x.", body="b", files=[_file("a.md", "AAA")])
    repo.get_draft_files.side_effect = SkillFileMissing("staged file gone")

    with pytest.raises(SkillRejected) as e:
        await svc.save_draft("u1", "a1", "abcd")

    assert "staged file gone" in str(e.value)
    repo.save_version.assert_not_awaited()


async def test_save_draft_missing_file_at_save_version_rejected(svc, repo):
    repo.get_draft.return_value = Skill(name="fs", description="Use when x.", body="b", files=[_file("a.md", "AAA")])
    repo.get_draft_files.return_value = {sha256_text("AAA"): "AAA"}
    repo.save_version.side_effect = SkillFileMissing("inherited file gone")

    with pytest.raises(SkillRejected) as e:
        await svc.save_draft("u1", "a1", "abcd")

    assert "inherited file gone" in str(e.value)


# ---------------------------------------------------------------------------
# list_skills
# ---------------------------------------------------------------------------

async def test_list_skills_still_shadows_system(svc, repo, caplog):
    custom = Skill(name="skill-creator", description="Custom.", body="Custom body.")
    other = Skill(name="alpha", description="A.", body="a")
    repo.list_current.return_value = [custom, other]

    result = await svc.list_skills("u1")

    assert [s.name for s in result] == ["alpha", "skill-creator"]
    assert result[1] is custom


# ---------------------------------------------------------------------------
# save (owner seed) — files inherit from the current version (RFC §15.3)
# ---------------------------------------------------------------------------

async def test_save_without_files_keeps_current_manifest(svc, repo):
    a = _file("references/a.md", "AAA")
    repo.list_current.return_value = [Skill(name="fs", description="Use when x.", body="old", version=1, files=[a])]

    await svc.save("u1", "a1", Skill(name="fs", description="Use when x.", body="new body"))

    saved = repo.save_version.call_args.args[2]
    assert saved.body == "new body"
    assert saved.files == [a]


async def test_save_of_new_skill_saves_as_given(svc, repo):
    repo.list_current.return_value = []

    await svc.save("u1", "a1", NEW)

    repo.save_version.assert_awaited_once_with("u1", "a1", NEW, cap=MAX_CUSTOM_SKILLS_PER_USER)


async def test_save_with_own_files_does_not_inherit(svc, repo):
    a = _file("references/a.md", "AAA")
    b = _file("references/b.md", "BBB")
    repo.list_current.return_value = [Skill(name="fs", description="Use when x.", body="old", version=1, files=[a])]
    incoming = Skill(name="fs", description="Use when x.", body="b", files=[b])

    await svc.save("u1", "a1", incoming)

    assert repo.save_version.call_args.args[2].files == [b]
