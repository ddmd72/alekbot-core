"""The indexer's Search v2 model wiring (USER_DRIVE_INDEXING_RFC §25.4.4): `build_drive_models` builds the reader, the
triage, the profiler and the passages runners over ONE context builder (one breaker per provider), one prompt
builder and one quota service; `build_drive_model_extractors` keeps its v1 shape on top of it."""
from unittest.mock import MagicMock

from src.adapters.firestore_quota_service import FirestoreQuotaService
from src.composition.drive_model_setup import DriveModels, build_drive_model_extractors, build_drive_models
from src.composition.drive_passage_runner import DrivePassageRunner
from src.composition.drive_profiler_runner import DriveProfilerRunner
from src.composition.drive_reader_runner import DriveReaderRunner
from src.composition.drive_triage_runner import DriveTriageRunner
from src.domain.drive_extraction import PARSER, READER

KEYS = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "GEMINI_API_KEY": "g"}


def _models(parser=None) -> DriveModels:
    return build_drive_models(parser=parser or MagicMock(), users=MagicMock(), account_repo=MagicMock(),
                              assembly_service=MagicMock(), keys=KEYS, alert_sink=None)


def test_every_runner_shares_one_context_prompt_and_quota():
    models = _models()
    reader = models.extractors[READER]
    runners = [reader, models.triage, models.profiler, models.passages]
    assert isinstance(reader, DriveReaderRunner) and isinstance(models.triage, DriveTriageRunner)
    assert isinstance(models.profiler, DriveProfilerRunner) and isinstance(models.passages, DrivePassageRunner)
    assert len({id(r._context_builder) for r in runners}) == 1
    assert len({id(r._prompt_builder) for r in runners}) == 1
    assert len({id(r._quota_service) for r in runners}) == 1
    assert isinstance(models.profiler._quota_service, FirestoreQuotaService)


def test_the_v1_builder_keeps_its_shape():
    parser = MagicMock()
    extractors, triage = build_drive_model_extractors(parser=parser, users=MagicMock(), account_repo=MagicMock(),
                                                      assembly_service=MagicMock(), keys=KEYS, alert_sink=None)
    assert set(extractors) == {PARSER, READER} and extractors[PARSER] is parser
    assert isinstance(triage, DriveTriageRunner)
