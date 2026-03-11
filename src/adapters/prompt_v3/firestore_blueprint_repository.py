"""
FirestoreBlueprintRepository — Firestore adapter for blueprint storage.

Part of Prompt Design System v4 (RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md).
"""

import logging
from typing import List

from google.cloud import firestore

from src.ports.prompt_v3.blueprint_repository import BlueprintRepository
from src.domain.prompt_v3.blueprint import Blueprint

logger = logging.getLogger(__name__)


class FirestoreBlueprintRepository(BlueprintRepository):
    """Firestore adapter for blueprint storage.

    Data Model (v4):
        {
            "blueprint_id": "universal_agent_v1",
            "outer_class": "Alek extends Agent",
            "class_order": ["properties", "cognitive_process", "policies",
                            "protocols", "output_format", "final_directives"]
        }

    Examples:
        >>> repo = FirestoreBlueprintRepository(db, "development_domain_prompt_blueprints_v3")
        >>> blueprint = await repo.get("universal_agent_v1")
        >>> blueprint.class_order
        ['properties', 'cognitive_process', ...]
    """

    def __init__(self, db: firestore.Client, collection_name: str):
        self.db = db
        self.collection_name = collection_name

    async def get(self, blueprint_id: str) -> Blueprint:
        doc_ref = self.db.collection(self.collection_name).document(blueprint_id)
        doc = await doc_ref.get()

        if not doc.exists:
            raise KeyError(f"Blueprint not found: {blueprint_id}")

        data = doc.to_dict()
        blueprint = Blueprint(
            id=data["blueprint_id"],
            outer_class=data["outer_class"],
            class_order=list(data["class_order"]),
        )
        blueprint.validate()
        return blueprint

    async def list_all(self) -> List[Blueprint]:
        docs = self.db.collection(self.collection_name).stream()
        blueprints: List[Blueprint] = []

        async for doc in self._iterate(docs):
            data = doc.to_dict()
            blueprint = Blueprint(
                id=data["blueprint_id"],
                outer_class=data["outer_class"],
                class_order=list(data["class_order"]),
            )
            blueprints.append(blueprint)

        return blueprints

    async def save(self, blueprint: Blueprint) -> None:
        blueprint.validate()

        data = {
            "blueprint_id": blueprint.id,
            "outer_class": blueprint.outer_class,
            "class_order": list(blueprint.class_order),
        }

        doc_ref = self.db.collection(self.collection_name).document(blueprint.id)
        doc_ref.set(data)
        logger.info(f"Saved blueprint: {blueprint.id}")

    async def delete(self, blueprint_id: str) -> None:
        doc_ref = self.db.collection(self.collection_name).document(blueprint_id)
        doc = await doc_ref.get()

        if not doc.exists:
            raise KeyError(f"Blueprint not found: {blueprint_id}")

        doc_ref.delete()
        logger.info(f"Deleted blueprint: {blueprint_id}")

    async def exists(self, blueprint_id: str) -> bool:
        doc_ref = self.db.collection(self.collection_name).document(blueprint_id)
        doc = await doc_ref.get()
        return doc.exists

    async def _iterate(self, stream):
        """Handle both sync and async Firestore streams."""
        if hasattr(stream, "__aiter__"):
            async for doc in stream:
                yield doc
        else:
            for doc in stream:
                yield doc
