"""The main graph's document service (EXTERNAL_FILES_RFC §4.4; U3 core): attached to the file conversion service, over
the container's context builder and assembly service, the graph's one quota service and user repository, the graph's
transcriber and a transcript cache on its own collection. Built over the sealed deploy environment."""
import pytest

from tests.support.prod_graph import prod_graph  # noqa: F401  (the fixture)


@pytest.mark.slow
async def test_the_document_service_is_attached_over_the_graphs_pieces(prod_graph):
    from src.adapters.firestore_transcript_cache import FirestoreTranscriptCache
    from src.services.document_service import DocumentService

    app = prod_graph.app
    documents = app.container.file_conversion_service._documents
    assert isinstance(documents, DocumentService)
    reader = documents._reader
    assert reader._context_builder is app.container.context_builder
    assert reader._prompt_builder.assembly_service is app.container.assembly_service
    assert reader._user_repo is app.user_repo
    assert documents._quota is reader._quota_service is app.quota_service
    assert isinstance(documents._transcript_cache, FirestoreTranscriptCache)
    assert documents._transcript_cache._collection.endswith("audio_transcripts")
    assert documents._transcriber is not None
