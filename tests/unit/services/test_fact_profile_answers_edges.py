"""Two failure edges of the profile step (fact profile RFC §4.2): an embedding batch of the wrong size is an error run
with nothing written. Invented cast."""
from tests.unit.services.test_fact_profile_service import CAR, World, answer, fid, ok


async def test_an_embedding_batch_of_the_wrong_size_is_an_error_run_with_nothing_written():
    world = World()
    await world.seed(CAR)
    world.profiler.replies[CAR] = ok(answer(label_words=["vehicle cover"]))

    async def short(texts, task_type="RETRIEVAL_DOCUMENT"):
        return []
    world.embedder.get_embeddings_batch = short

    run = await world.run()

    assert run.error and world.docs[fid(CAR)]["profile_fingerprint"] is None
