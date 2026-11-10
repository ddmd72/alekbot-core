"""The place atlas is one per process (fact profile RFC §4.7, PR 5e): the profile step and the owner's word replies read
the atlas the composition root passes them, and a step built without one loads its own."""
from types import SimpleNamespace

from src.composition.fact_profile_setup import LazyAtlas, build_fact_profile_service

CONTAINER = SimpleNamespace(repository=object(), fact_profiler=object(), embedding_service=object(),
                            confirmed_people=object(), profile_answers=object())


def test_the_step_reads_the_atlas_it_is_given():
    atlas = LazyAtlas()

    assert build_fact_profile_service(CONTAINER, max_per_run=1, atlas=atlas)._atlas is atlas


def test_a_step_built_without_an_atlas_loads_its_own():
    first = build_fact_profile_service(CONTAINER, max_per_run=1)
    second = build_fact_profile_service(CONTAINER, max_per_run=1)

    assert isinstance(first._atlas, LazyAtlas) and first._atlas is not second._atlas
