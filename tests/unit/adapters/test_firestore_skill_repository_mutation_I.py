"""Mutation-survivor kill: the drafts read of `save_version` happens inside the transaction."""
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.firestore_skill_repository import FirestoreSkillRepository
from src.config.environment import EnvironmentConfig
from src.domain.skill import Skill


async def test_consumed_drafts_are_read_inside_the_save_transaction():
    env = MagicMock(spec=EnvironmentConfig)
    env.skills_collection = "test_skills"
    env.skill_drafts_collection = "test_skill_drafts"
    col, col_drafts = MagicMock(), MagicMock()
    db = MagicMock()
    db.collection.side_effect = lambda name: {"test_skills": col, "test_skill_drafts": col_drafts}[name]

    doc_ref = MagicMock()
    missing = MagicMock(exists=False)
    doc_ref.get = AsyncMock(return_value=missing)
    col.document.return_value = doc_ref
    col.where.return_value.get = AsyncMock(return_value=[])

    drafts_get = AsyncMock(return_value=[])
    chained = MagicMock()
    chained.where.return_value.get = drafts_get
    col_drafts.where.return_value = chained

    txn = MagicMock()
    db.transaction.return_value = txn

    repo = FirestoreSkillRepository(db, env)
    skill = Skill(name="flight-status", description="Use when x.", body="1. Open.")
    with patch("src.adapters.firestore_skill_repository.firestore.async_transactional", side_effect=lambda fn: fn):
        await repo.save_version("u1", "a1", skill, cap=20, consume_drafts_named="flight-status")

    drafts_get.assert_awaited_once_with(transaction=txn)
