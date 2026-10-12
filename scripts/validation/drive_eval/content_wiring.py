"""
The eval's real content ports (USER_DRIVE_INDEXING_RFC §24.2, §24.11): the child-process parser, the document reader
and the image triage, built as the indexer service builds them, minus billing (no quota service: the eval user is not
an account). The prompts come from Firestore, so a run needs GCP credentials and the owner-uploaded drive_reader /
drive_triage prompts; the model keys come from .env. The reader runs on the eval user's default provider (Claude ECO,
§24.2), or on `DRIVE_EVAL_PROVIDER=openai` for the other leg of the model check.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Tuple

from src.domain.drive_extraction import PARSER, READER
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.image_triage_port import ImageTriagePort


class ContentSetupError(RuntimeError):
    """The real content ports could not be built (a missing key, credentials or prompt store)."""


class _EvalUsers:
    """The eval user as the runners read it: indexing on, the provider the run asks for (§24.2 D7)."""

    def __init__(self, provider: str) -> None:
        self._provider = provider

    async def get_user(self, user_id: str) -> Any:
        from src.domain.user import UserBotConfig, UserProfile

        providers = {"drive_reader": self._provider, "drive_triage": self._provider} if self._provider else None
        return UserProfile(user_id=user_id, account_id="eval-account",
                           config=UserBotConfig(drive_indexing_enabled=True, agent_providers=providers))


def build_real_content() -> Tuple[Dict[str, ContentExtractorPort], ImageTriagePort]:
    """{PARSER, READER} and the triage port. Raises ContentSetupError instead of falling back: bars recorded without
    the reader would be meaningless."""
    provider = (os.getenv("DRIVE_EVAL_PROVIDER") or "").strip()
    needed = {"": "ANTHROPIC_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
    if needed is None:
        raise ContentSetupError(f"DRIVE_EVAL_PROVIDER must be claude or openai, not {provider!r}")
    if not os.getenv(needed):
        raise ContentSetupError(f"{needed} is not set (.env)")
    try:
        from google.cloud import firestore

        from src.adapters.firestore_account_repo import FirestoreAccountRepository
        from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
        from src.composition.drive_reader_runner import DriveReaderRunner
        from src.composition.drive_triage_runner import DriveTriageRunner
        from src.composition.service_container import ServiceContainer
        from src.config.settings import load_settings
        from src.services.prompt_builder import PromptBuilder

        db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
        settings = load_settings()
        env_config = settings["ENVIRONMENT_CONFIG"]
        account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
        container = ServiceContainer(config=settings, db_client=db, env_config=env_config, account_repo=account_repo)
        prompt_builder = PromptBuilder(repo=None, assembly_service=container.assembly_service)
        users = _EvalUsers(provider)
        parser = SubprocessContentExtractor()
        reader = DriveReaderRunner(context_builder=container.context_builder, user_repo=users,
                                   prompt_builder=prompt_builder, splitter=parser, quota_service=None)
        triage = DriveTriageRunner(context_builder=container.context_builder, user_repo=users,
                                   prompt_builder=prompt_builder, quota_service=None)
    except Exception as exc:   # noqa: BLE001 — any setup failure is "no verdict", never a silent fallback
        raise ContentSetupError(f"content ports could not be built: {type(exc).__name__}: {exc}") from exc
    return {PARSER: parser, READER: reader}, triage
