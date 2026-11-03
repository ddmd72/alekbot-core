"""Mutation-survivor kill: `find_nearest` restricted to one list filters on list_id."""
from unittest.mock import AsyncMock, MagicMock

from google.cloud.firestore import FieldFilter

from src.adapters.firestore_task_search_index import FirestoreTaskSearchIndex
from src.config.environment import EnvironmentConfig


async def test_find_nearest_with_a_list_filters_the_query_on_that_list():
    filters = []

    class _Query:
        def where(self, filter: FieldFilter):
            filters.append((filter.field_path, filter.op_string, filter.value))
            return self

        def find_nearest(self, **kwargs):
            q = MagicMock()
            q.get = AsyncMock(return_value=[])
            return q

    env = MagicMock(spec=EnvironmentConfig)
    env.task_search_index_collection = "test_task_search_index"
    db = MagicMock()
    db.collection.return_value = _Query()
    index = FirestoreTaskSearchIndex(db, env)

    await index.find_nearest("user-abc", {"content": [0.1, 0.2]}, list_id="list-1")

    assert ("list_id", "==", "list-1") in filters
