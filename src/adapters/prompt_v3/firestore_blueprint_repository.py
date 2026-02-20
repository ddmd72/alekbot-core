"""
FirestoreBlueprintRepository - Firestore adapter for blueprint storage.

Part of Prompt Design System v3 (RFC).
"""

import logging
from typing import List

from google.cloud import firestore

from src.ports.prompt_v3.blueprint_repository import BlueprintRepository
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.slot import BlueprintClass, OwnerType
from src.domain.prompt_v3.token import TokenId, TokenCategory

logger = logging.getLogger(__name__)


class FirestoreBlueprintRepository(BlueprintRepository):
    """Firestore adapter for blueprint storage.

    Data Model:
        {
            "blueprint_id": "smart_agent_v1",
            "template": "class Alek { {{HUMOR_ENGINE}} {{VOICE}} }",
            "classes": {
                "HUMOR_ENGINE": {
                    "allowed_token_categories": ["humor_engine"],
                    "overridable_by": ["USER"],
                    "default_token": "HUMOR_PRESET_RANEVSKAYA"
                },
                "VOICE": {
                    "allowed_token_categories": ["voice", "tone"],
                    "overridable_by": ["ACCOUNT", "USER"],
                    "default_token": "VOICE_CONVERSATIONAL"
                }
            }
        }

    Examples:
        >>> from google.cloud import firestore
        >>> db = firestore.Client()
        >>> repo = FirestoreBlueprintRepository(db, "dev_prompt_blueprints_v3")
        >>> blueprint = await repo.get("smart_agent_v1")
    """

    def __init__(self, db: firestore.Client, collection_name: str):
        """Initialize Firestore blueprint repository.

        Args:
            db: Firestore client instance
            collection_name: Collection name (e.g., "dev_prompt_blueprints_v3")
        """
        self.db = db
        self.collection_name = collection_name

    async def get(self, blueprint_id: str) -> Blueprint:
        """Fetch blueprint by ID.

        Args:
            blueprint_id: Blueprint identifier (e.g., "smart_agent_v1")

        Returns:
            Blueprint instance

        Raises:
            KeyError: If blueprint not found
        """
        doc_ref = self.db.collection(self.collection_name).document(blueprint_id)
        doc = await doc_ref.get()

        if not doc.exists:
            raise KeyError(f"Blueprint not found: {blueprint_id}")

        data = doc.to_dict()
        classes = self._deserialize_classes(data.get("classes", {}))

        blueprint = Blueprint(
            id=data["blueprint_id"],
            classes=classes,
            template=data["template"]
        )

        blueprint.validate()
        return blueprint

    async def list_all(self) -> List[Blueprint]:
        """List all blueprints.

        Returns:
            List of all blueprints
        """
        docs = self.db.collection(self.collection_name).stream()
        blueprints: List[Blueprint] = []

        for doc in docs:
            data = doc.to_dict()
            classes = self._deserialize_classes(data.get("classes", {}))

            blueprint = Blueprint(
                id=data["blueprint_id"],
                classes=classes,
                template=data["template"]
            )

            blueprints.append(blueprint)

        return blueprints

    async def save(self, blueprint: Blueprint) -> None:
        """Save blueprint to repository.

        Args:
            blueprint: Blueprint instance to save
        """
        blueprint.validate()

        classes_data = self._serialize_classes(blueprint.classes)

        data = {
            "blueprint_id": blueprint.id,
            "template": blueprint.template,
            "classes": classes_data
        }

        doc_ref = self.db.collection(self.collection_name).document(blueprint.id)
        doc_ref.set(data)
        logger.info(f"Saved blueprint: {blueprint.id}")

    async def delete(self, blueprint_id: str) -> None:
        """Delete blueprint from repository.

        Args:
            blueprint_id: Blueprint ID to delete

        Raises:
            KeyError: If blueprint not found
        """
        doc_ref = self.db.collection(self.collection_name).document(blueprint_id)
        doc = await doc_ref.get()

        if not doc.exists:
            raise KeyError(f"Blueprint not found: {blueprint_id}")

        doc_ref.delete()
        logger.info(f"Deleted blueprint: {blueprint_id}")

    async def exists(self, blueprint_id: str) -> bool:
        """Check if blueprint exists.

        Args:
            blueprint_id: Blueprint ID to check

        Returns:
            True if blueprint exists, False otherwise
        """
        doc_ref = self.db.collection(self.collection_name).document(blueprint_id)
        doc = await doc_ref.get()
        return doc.exists

    def _deserialize_classes(self, classes_data: dict) -> dict[str, BlueprintClass]:
        classes = {}

        for class_name, class_data in classes_data.items():
            classes[class_name] = BlueprintClass(
                allowed_token_categories={
                    TokenCategory(cat) for cat in class_data["allowed_token_categories"]
                },
                overridable_by={
                    OwnerType(owner) for owner in class_data["overridable_by"]
                },
                default_token=TokenId(class_data["default_token"])
            )

        return classes

    def _serialize_classes(self, classes: dict[str, BlueprintClass]) -> dict:
        classes_data = {}

        for class_name, class_schema in classes.items():
            classes_data[class_name] = {
                "allowed_token_categories": [
                    str(cat) for cat in class_schema.allowed_token_categories
                ],
                "overridable_by": [owner.value for owner in class_schema.overridable_by],
                "default_token": str(class_schema.default_token)
            }

        return classes_data