"""The review benches can measure quote translations: the owner's fixed language is forceable, the summary counts the
translations a run kept, the kit store lists them, and the answers bench has a Ukrainian-quote case it can run alone."""
from types import SimpleNamespace

import pytest

from scripts.consolidation import review_bench_cases as cases
from scripts.consolidation import review_bench_support as support
from scripts.consolidation import test_review_answers_dryrun as answers_bench
from scripts.consolidation import test_review_dryrun as review_bench
from scripts.consolidation.review_kit_store import RecordingReviewStore
from src.domain.fact_review_frame import reply_part, review_frame
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.domain.language import LanguageCode
from src.domain.user import UserBotConfig, UserProfile
from tests.support.fact_kit import ACC, USER
from tests.support.fact_lifecycle_world import NOW
from tests.unit.scripts.test_review_kit import LISBON


class Profile:
    async def get_user(self, user_id: str):
        return UserProfile(user_id=user_id, account_id=ACC, config=UserBotConfig(
            timezone=LISBON, preferred_language=LanguageCode.FR, agent_mirror=True))


class Live:
    async def get_primary(self, user_id: str):
        return None


class Bundle:
    def __init__(self) -> None:
        self.language = SimpleNamespace(preferences={})
        self.notification_state = SimpleNamespace()


async def test_seed_owner_can_force_a_fixed_language():
    bundle = Bundle()

    seeded = await support.seed_owner(bundle, user_id=USER, user_repo=Profile(), live_state=Live(), fixed=LanguageCode.UK)

    assert (seeded["mirror"], seeded["fixed_language"]) == (False, "uk")
    assert bundle.language.preferences[USER] == (LanguageCode.UK, False)
    with pytest.raises(ValueError):
        await support.seed_owner(bundle, user_id=USER, user_repo=Profile(), live_state=Live(), mirror="ru",
                                 fixed=LanguageCode.UK)


def test_the_summary_counts_translations_and_tolerates_old_runs():
    new = {"owner": {}, "translations": [{"language": "uk", "source_digest": "x", "shown": "a"},
                                         {"language": "uk", "source_digest": "y", "shown": "b"}]}
    old = {"owner": {}}      # a run recorded before translations existed

    lines = support.review_summary({"passes": []}, {"runs": [new, old]})

    assert "run 1: 2 translation(s) kept (uk)" in lines
    assert "run 2: 0 translation(s) kept (none)" in lines
    assert not any("a" == line or "b" == line for line in lines)


async def test_the_kit_store_lists_quote_translation_documents():
    store = RecordingReviewStore()
    record = QuoteTranslation("uk", quote_digest("Alex keeps a log"), "Алекс веде журнал")

    await store.set_quote_translations("acc", [record], NOW)
    await store.claim_digest("u1", NOW.date(), NOW)

    [doc] = store.quote_translation_docs()
    assert (doc["language"], doc["source_digest"], doc["text"]) == ("uk", record.source_digest, record.text)
    doc["text"] = "changed"
    assert store.quote_translation_docs()[0]["text"] == record.text      # copies


def test_the_translated_correction_case_quotes_in_ukrainian():
    case = next(c for c in cases.answer_cases() if c.name == "correction_translated_quote")
    qid = case.question_id

    assert case.window[0] == ("user", review_frame(qid))
    role, model_text = case.window[1]
    assert role == "model" and cases.QUOTED_UK in model_text and "Це досі правда?" in model_text
    assert model_text.splitlines()[1] == f"«{cases.QUOTED_UK}»"
    assert case.window[1] == cases.translated_question_pair(qid)[1]
    reply = case.window[2]
    assert reply == ("user", reply_part(qid, "3 — it's Iron Temple now"))
    assert not any("Ѐ" <= ch <= "ӿ" for ch in reply[1])
    assert case.rule == cases.ONE_CREATE and case.contains == "Iron Temple"


def test_the_answers_bench_can_run_one_case():
    args = answers_bench.build_parser().parse_args(["--s3-kit", "--case", "correction_translated_quote", "--case", "digit_reply"])
    assert args.case == ["correction_translated_quote", "digit_reply"]
    assert [c.name for c in answers_bench.selected_cases(args.case)] == ["digit_reply", "correction_translated_quote"]
    assert len(answers_bench.selected_cases(None)) == len(cases.answer_cases())
    assert answers_bench.estimate_text(2) == "about $0.15 on Sonnet, $0.30 on Opus"
    with pytest.raises(SystemExit):
        answers_bench.build_parser().parse_args(["--s3-kit", "--case", "no_such_case"])


def test_the_review_bench_takes_a_fixed_language_but_not_with_mirror():
    parsed = review_bench.build_parser().parse_args(["--s3-kit", "--fixed", "uk"])
    assert parsed.fixed == "uk" and parsed.mirror is None
    with pytest.raises(SystemExit):
        review_bench.build_parser().parse_args(["--s3-kit", "--fixed", "uk", "--mirror", "ru"])
    with pytest.raises(SystemExit):
        review_bench.build_parser().parse_args(["--s3-kit", "--fixed", "pt"])
