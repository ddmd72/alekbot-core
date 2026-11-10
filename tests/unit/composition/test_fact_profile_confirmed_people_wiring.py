"""The confirmed people list reaches the profile step in production (FACT_PROFILE_RFC §4.5): the graph's one profile
step reads the list through the container's repository, the same object every builder of the step uses, so a step
built without it cannot ship silently."""
import gc

import pytest

from src.adapters.firestore_confirmed_people_repo import FirestoreConfirmedPeopleRepository
from src.ports.confirmed_people_repository import ConfirmedPeopleRepository
from src.services.fact_profile_service import FactProfileService
from tests.support.prod_graph import prod_graph  # noqa: F401  (fixture)

pytestmark = pytest.mark.slow


async def test_the_profile_step_holds_the_containers_confirmed_people_repository(prod_graph):
    container = prod_graph.app.container
    steps = [o for o in gc.get_objects() if isinstance(o, FactProfileService)
             and getattr(o, "_repo", None) is container.repository]

    assert len(steps) == 1
    assert isinstance(container.confirmed_people, FirestoreConfirmedPeopleRepository)
    assert isinstance(container.confirmed_people, ConfirmedPeopleRepository)
    assert steps[0]._confirmed_people is container.confirmed_people
