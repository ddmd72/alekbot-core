"""The eval's real content ports build against the real runner and parser constructors (USER_DRIVE_INDEXING_RFC
§24.11): GCP, settings and the service container are stubbed, the parser, reader and triage classes are not."""
from types import SimpleNamespace

from scripts.validation.drive_eval import content_wiring
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.composition.drive_reader_runner import DriveReaderRunner
from src.composition.drive_triage_runner import DriveTriageRunner
from src.domain.drive_extraction import PARSER, READER


def test_build_real_content_wires_the_parser_as_the_readers_splitter(monkeypatch):
    import google.cloud.firestore as firestore_mod
    import src.adapters.firestore_account_repo as account_mod
    import src.composition.service_container as container_mod
    import src.config.settings as settings_mod

    container = SimpleNamespace(context_builder=object(), assembly_service=object())
    monkeypatch.setenv("DRIVE_EVAL_PROVIDER", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline")
    monkeypatch.setattr(firestore_mod, "AsyncClient", lambda **kwargs: object())
    monkeypatch.setattr(settings_mod, "load_settings",
                        lambda: {"ENVIRONMENT_CONFIG": SimpleNamespace(account_collection_name="accounts")})
    monkeypatch.setattr(account_mod, "FirestoreAccountRepository", lambda **kwargs: object())
    monkeypatch.setattr(container_mod, "ServiceContainer", lambda **kwargs: container)

    extractors, triage = content_wiring.build_real_content()

    parser, reader = extractors[PARSER], extractors[READER]
    assert isinstance(parser, SubprocessContentExtractor)
    assert isinstance(reader, DriveReaderRunner) and reader.extractor_id == READER
    assert reader._splitter is parser   # one sandboxed parser both extracts and cuts the reader's PDF windows
    assert isinstance(triage, DriveTriageRunner)
