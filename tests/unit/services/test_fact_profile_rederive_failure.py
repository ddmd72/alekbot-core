"""A stored profile answer that no longer passes the derive check is a failure, never a re-derived write."""
from src.domain.fact_profile_fingerprint import profile_fingerprint
from tests.unit.services.test_fact_profile_service import UNION, World, answer, fid, ok


async def test_a_stored_answer_that_now_fails_the_derive_check_is_a_failure_not_a_rederivation():
    world = World()
    await world.seed(UNION)
    world.profiler.replies[UNION] = ok(answer(label_words=["union dues"]))
    await world.profile_everything()
    world.profiler.calls.clear()
    doc = world.docs[fid(UNION)]
    doc["profile_derivation"] = "old"
    doc["profile_answer"] = answer(sensitivity="intimate", label_words=[]).model_dump(mode="json")
    labels_before = list(doc["label_words"])

    run = await world.run()

    assert world.profiler.calls == [] and (run.rederived, run.failed) == (0, 1)
    assert run.drops.get("intimate_without_labels") == 1
    doc = world.docs[fid(UNION)]
    assert doc["profile_failures"] == 1 and doc["profile_failed_fingerprint"] == profile_fingerprint(UNION, None)
    assert doc["profile_derivation"] == "old" and doc["label_words"] == labels_before
