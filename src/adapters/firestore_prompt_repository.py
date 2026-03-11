"""
Firestore implementation of PromptComponentRepository.

Session: 23 (Prompt Component Architecture Implementation - facts storage)
Session: 25 (Integration with 3-level priority system - prompt_components collection)
RFC: docs/architecture/rfcs/PROMPT_COMPONENT_ARCHITECTURE_RFC.md

Changes in SESSION_25:
- Collection: facts → prompt_components
- Query structure: lineage_id pattern → component_id + owner_type + owner_value
- Added 3-level resolution: USER > AGENT > SYSTEM
- Added fallthrough pattern (empty content)
- Added exclusion pattern (is_enabled=False)
"""

from typing import List, Optional
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from src.domain.prompt import PromptComponent, ComponentScope, OwnerType
from src.ports.prompt_component_repository import PromptComponentRepository
from src.utils.logger import logger


class FirestorePromptComponentRepository(PromptComponentRepository):
    """
    Firestore adapter for prompt component storage.
    
    SESSION_25 Storage strategy:
    - Collection: {env_prefix}prompt_components (not facts!)
    - Document structure:
        {
            "component_id": "cognitive_process",
            "owner_type": "SYSTEM" | "AGENT" | "USER",
            "owner_value": null | "smart" | "user_uuid",
            "scope": "class.Alek",
            "order": 10,
            "text": "...",
            "is_enabled": true,
            "version": "1.0"
        }
    - Resolution: 3-level priority (USER > AGENT > SYSTEM)
    - Fallthrough: empty text = skip to next level
    - Exclusion: is_enabled=false = remove component
    """
    
    def __init__(self, db_client: firestore.AsyncClient, collection_name: str = "prompt_components"):
        """
        Initialize repository.
        
        Args:
            db_client: Firestore async client
            collection_name: Collection name (default: "prompt_components")
                            Use with env prefix: "{env}_prompt_components"
        """
        self.db = db_client
        self.collection = db_client.collection(collection_name)
        logger.info(f"📦 FirestorePromptComponentRepository initialized: {collection_name}")
    
    async def get_default_components(self, scope: Optional[ComponentScope] = None) -> List[PromptComponent]:
        """
        Retrieve all default (SYSTEM) prompt components.
        
        SESSION_25: Queries owner_type=SYSTEM only.
        
        Args:
            scope: Optional filter by component scope
            
        Returns:
            List of SYSTEM components, sorted by order
        """
        query = (
            self.collection
            .where(filter=FieldFilter("owner_type", "==", "SYSTEM"))
        )
        
        components = []
        async for doc in query.stream():
            data = doc.to_dict()
            
            # Parse scope
            comp_scope_str = data.get("scope")
            try:
                comp_scope = ComponentScope(comp_scope_str) if comp_scope_str else None
            except ValueError:
                logger.warning(f"Invalid scope '{comp_scope_str}' for component {doc.id}")
                continue
            
            # Filter by scope if specified
            if scope and comp_scope != scope:
                continue
            
            component = PromptComponent(
                id=data.get("component_id", doc.id),
                scope=comp_scope,
                content=data.get("text", ""),
                order=data.get("order", 999),
                owner_type=OwnerType.SYSTEM,
                owner_value=data.get("owner_value"),
                is_enabled=data.get("is_enabled", True),
                version=data.get("version", "1.0")
            )
            
            components.append(component)
        
        # Sort by order
        components.sort(key=lambda c: c.order)
        
        logger.debug(f"📦 Loaded {len(components)} SYSTEM components" + 
                    (f" for scope {scope.value}" if scope else ""))
        
        return components
    
    async def resolve_component(
        self,
        component_id: str,
        agent_type: str,
        account_id: str,   # SESSION_26: REQUIRED (was Optional)
        user_id: str       # SESSION_26: REQUIRED (was Optional)
    ) -> Optional[PromptComponent]:
        """
        Resolve component using 4-level priority: USER > ACCOUNT > AGENT > SYSTEM.

        SESSION_25: Core resolution logic with 3 levels
        SESSION_26: Extended to 4 levels with ACCOUNT. Made user_id/account_id REQUIRED.

        Resolution order (ALWAYS executes all levels since IDs are required):
        1. Try USER level (user_id always present)
           - If is_enabled=False → return None (EXCLUDED)
           - If text="" → fallthrough to ACCOUNT
           - Else → return component
        2. Try ACCOUNT level (account_id always present) [NEW SESSION_26]
           - Same logic as USER
        3. Try AGENT level (agent_type)
           - Same logic
        4. Try SYSTEM level
           - Same logic
        5. If nothing found → return None

        NOTE: For unregistered users, use ANONYMOUS_USER_ID/ANONYMOUS_ACCOUNT_ID.
        For system operations, use SYSTEM_USER_ID/SYSTEM_ACCOUNT_ID.

        Args:
            component_id: Component identifier (e.g., "cognitive_process")
            agent_type: Agent type (e.g., "quick", "smart", "router")
            account_id: REQUIRED account identifier (use ANONYMOUS_ACCOUNT_ID if unreg)
            user_id: REQUIRED user identifier (use ANONYMOUS_USER_ID if unreg)

        Returns:
            Resolved PromptComponent or None if excluded/not found
        """

        # 1. Try USER level (always executes - user_id is required)
        query = (
            self.collection
            .where(filter=FieldFilter("component_id", "==", component_id))
            .where(filter=FieldFilter("owner_type", "==", "USER"))
            .where(filter=FieldFilter("owner_value", "==", user_id))
            .limit(1)
        )

        async for doc in query.stream():
            data = doc.to_dict()

            # Check exclusion
            if not data.get("is_enabled", True):
                logger.debug(f"🚫 Component '{component_id}' EXCLUDED by USER/{user_id[:8]}")
                return None

            # Check fallthrough
            if not data.get("text", "").strip():
                logger.debug(f"⬇️ Component '{component_id}' USER fallthrough (empty text)")
                break  # Go to ACCOUNT level

            # USER override found
            logger.debug(f"✅ Component '{component_id}' resolved from USER/{user_id[:8]}")
            return self._build_component(data, OwnerType.USER)

        # 2. Try ACCOUNT level (always executes - account_id is required)
        query = (
            self.collection
            .where(filter=FieldFilter("component_id", "==", component_id))
            .where(filter=FieldFilter("owner_type", "==", "ACCOUNT"))
            .where(filter=FieldFilter("owner_value", "==", account_id))
            .limit(1)
        )

        async for doc in query.stream():
            data = doc.to_dict()

            # Check exclusion
            if not data.get("is_enabled", True):
                logger.debug(f"🚫 Component '{component_id}' EXCLUDED by ACCOUNT/{account_id[:8]}")
                return None

            # Check fallthrough
            if not data.get("text", "").strip():
                logger.debug(f"⬇️ Component '{component_id}' ACCOUNT fallthrough (empty text)")
                break  # Go to AGENT level

            # ACCOUNT override found
            logger.debug(f"✅ Component '{component_id}' resolved from ACCOUNT/{account_id[:8]}")
            return self._build_component(data, OwnerType.ACCOUNT)

        # 3. Try AGENT level
        query = (
            self.collection
            .where(filter=FieldFilter("component_id", "==", component_id))
            .where(filter=FieldFilter("owner_type", "==", "AGENT"))
            .where(filter=FieldFilter("owner_value", "==", agent_type))
            .limit(1)
        )
        
        async for doc in query.stream():
            data = doc.to_dict()
            
            # Check exclusion
            if not data.get("is_enabled", True):
                logger.debug(f"🚫 Component '{component_id}' EXCLUDED by AGENT/{agent_type}")
                return None
            
            # Check fallthrough
            if not data.get("text", "").strip():
                logger.debug(f"⬇️ Component '{component_id}' AGENT fallthrough (empty text)")
                break  # Go to SYSTEM level
            
            # AGENT override found
            logger.debug(f"✅ Component '{component_id}' resolved from AGENT/{agent_type}")
            return self._build_component(data, OwnerType.AGENT)

        # 4. Try SYSTEM level
        query = (
            self.collection
            .where(filter=FieldFilter("component_id", "==", component_id))
            .where(filter=FieldFilter("owner_type", "==", "SYSTEM"))
            .limit(1)
        )
        
        async for doc in query.stream():
            data = doc.to_dict()
            
            # Check exclusion
            if not data.get("is_enabled", True):
                logger.debug(f"🚫 Component '{component_id}' EXCLUDED by SYSTEM")
                return None
            
            # SYSTEM component found
            logger.debug(f"✅ Component '{component_id}' resolved from SYSTEM")
            return self._build_component(data, OwnerType.SYSTEM)
        
        # Not found at any level
        logger.warning(f"⚠️ Component '{component_id}' not found in any level")
        return None
    
    def _build_component(self, data: dict, owner_type: OwnerType) -> PromptComponent:
        """Build PromptComponent from Firestore data."""
        comp_scope_str = data.get("scope")
        try:
            comp_scope = ComponentScope(comp_scope_str)
        except ValueError:
            logger.error(f"Invalid scope '{comp_scope_str}', using CLASS_ROOT as fallback")
            comp_scope = ComponentScope.CLASS_ROOT
        
        return PromptComponent(
            id=data.get("component_id", "unknown"),
            scope=comp_scope,
            content=data.get("text", ""),
            order=data.get("order", 999),
            owner_type=owner_type,
            owner_value=data.get("owner_value"),
            is_enabled=data.get("is_enabled", True),
            version=data.get("version", "1.0")
        )
    
    async def get_user_overrides(self, user_id: str, scope: Optional[ComponentScope] = None) -> List[PromptComponent]:
        """
        Retrieve user-specific component overrides.
        
        SESSION_25: Returns only USER level components (not resolved).
        For resolution, use resolve_component() instead.
        
        Args:
            user_id: User identifier
            scope: Optional filter by component scope
            
        Returns:
            List of USER components (may be empty, may be exclusions)
        """
        query = (
            self.collection
            .where(filter=FieldFilter("owner_type", "==", "USER"))
            .where(filter=FieldFilter("owner_value", "==", user_id))
        )
        
        components = []
        async for doc in query.stream():
            data = doc.to_dict()
            
            # Parse scope
            comp_scope_str = data.get("scope")
            try:
                comp_scope = ComponentScope(comp_scope_str) if comp_scope_str else None
            except ValueError:
                logger.warning(f"Invalid scope '{comp_scope_str}' for user component {doc.id}")
                continue
            
            # Filter by scope if specified
            if scope and comp_scope != scope:
                continue
            
            component = PromptComponent(
                id=data.get("component_id", doc.id),
                scope=comp_scope,
                content=data.get("text", ""),
                order=data.get("order", 999),
                owner_type=OwnerType.USER,
                owner_value=user_id,
                is_enabled=data.get("is_enabled", True),
                is_user_override=True,  # Legacy field
                version=data.get("version", "1.0")
            )
            
            components.append(component)
        
        components.sort(key=lambda c: c.order)
        
        logger.debug(f"📦 Loaded {len(components)} USER overrides for {user_id[:8]}" +
                    (f" for scope {scope.value}" if scope else ""))
        
        return components
    
    async def save_user_override(self, user_id: str, component: PromptComponent) -> None:
        """
        Save or update a user-specific component override.
        
        SESSION_25: Stores in prompt_components collection with owner_type=USER.
        
        Args:
            user_id: User identifier
            component: Component to save
        """
        # Check if override already exists
        query = (
            self.collection
            .where(filter=FieldFilter("component_id", "==", component.id))
            .where(filter=FieldFilter("owner_type", "==", "USER"))
            .where(filter=FieldFilter("owner_value", "==", user_id))
            .limit(1)
        )
        
        existing_docs = [doc async for doc in query.stream()]
        
        doc_data = {
            "component_id": component.id,
            "owner_type": "USER",
            "owner_value": user_id,
            "scope": component.scope.value,
            "order": component.order,
            "text": component.content,
            "is_enabled": component.is_enabled,
            "version": component.version,
            "updated_at": firestore.SERVER_TIMESTAMP
        }
        
        if existing_docs:
            # Update existing
            doc_ref = existing_docs[0].reference
            await doc_ref.update(doc_data)
            logger.info(f"✅ Updated USER override: {component.id} for {user_id[:8]}")
        else:
            # Create new
            doc_ref = self.collection.document()
            doc_data["created_at"] = firestore.SERVER_TIMESTAMP
            await doc_ref.set(doc_data)
            logger.info(f"✅ Created USER override: {component.id} for {user_id[:8]}")
    
    async def delete_user_override(self, user_id: str, component_id: str) -> bool:
        """
        Delete a user-specific component override.
        
        SESSION_25: Physically deletes document from Firestore.
        
        Args:
            user_id: User identifier
            component_id: Component identifier to delete
            
        Returns:
            True if deleted, False if not found
        """
        query = (
            self.collection
            .where(filter=FieldFilter("component_id", "==", component_id))
            .where(filter=FieldFilter("owner_type", "==", "USER"))
            .where(filter=FieldFilter("owner_value", "==", user_id))
            .limit(1)
        )
        
        existing_docs = [doc async for doc in query.stream()]
        
        if existing_docs:
            # Physical delete
            doc_ref = existing_docs[0].reference
            await doc_ref.delete()
            logger.info(f"🗑️ Deleted USER override: {component_id} for {user_id[:8]}")
            return True
        else:
            logger.warning(f"⚠️ USER override not found: {component_id} for {user_id[:8]}")
            return False


# =============================================================================
# LEGACY CODE (Session 23 - facts storage)
# =============================================================================
# Kept for reference. DO NOT USE. Use SESSION_25 code above.
#
# class FirestorePromptComponentRepository_LEGACY:
#     """OLD implementation using 'facts' collection with lineage_id pattern."""
#     
#     def __init__(self, db_client, collection_name="facts"):
#         self.collection = db_client.collection(collection_name)
#     
#     async def get_default_components(self, scope=None):
#         # Query: owner_id="SYSTEM", lineage_id="prompt_component_*"
#         # Metadata: component_type="groovy_block"
#         pass
#     
#     async def get_user_overrides(self, user_id, scope=None):
#         # Query: owner_id=user_id, lineage_id="prompt_component_*"
#         pass
#
# =============================================================================
