from typing import List, Optional
from google.cloud.firestore import FieldFilter
from ..domain.invite_code import InviteCode, InviteType, invite_hint, normalise_invite_code
from ..domain.secret_digest import keyed_digest
from ..ports.invite_code_repository import InviteCodeRepository
from ..config.environment import EnvironmentConfig
from ..utils.logger import logger
from ..utils.timer import log_execution_time


_PURPOSE = "invite"


class FirestoreInviteCodeRepository(InviteCodeRepository):
    """
    Firestore implementation of InviteCodeRepository.

    The doc id is the keyed digest of the normalised code (SECRETS_AT_REST_RFC §2.5); no field holds the code, only
    its non-secret hint. A lookup rebuilds the invite from the presented value.
    """

    def __init__(self, db_client, env_config: EnvironmentConfig, *, digest_key: bytes):
        self._digest_key = digest_key
        self.db = db_client
        self.env_config = env_config
        
        # Use dynamic collection name
        collection_name = env_config.domain_invite_codes_collection
        self.collection = self.db.collection(collection_name)
        
        logger.info(f"📂 InviteCode Repository initialized. Collection: {collection_name}")

    def _to_firestore(self, invite_code: InviteCode) -> dict:
        """Convert InviteCode domain entity to Firestore document."""
        data = {
            "hint": invite_code.hint or (invite_hint(invite_code.code) if invite_code.code else None),
            "user_id": invite_code.user_id,
            "account_id": invite_code.account_id,
            "type": invite_code.type.value,
            "expires_at": invite_code.expires_at,
            "created_at": invite_code.created_at,
            "role": invite_code.role,
        }
        
        if invite_code.platform:
            data["platform"] = invite_code.platform
            
        if invite_code.used_at:
            data["used_at"] = invite_code.used_at
            
        if invite_code.used_by_user_id:
            data["used_by_user_id"] = invite_code.used_by_user_id
            
        return data

    def _doc_id(self, code: Optional[str]) -> str:
        if not code:
            raise ValueError("An invite code is required to address its document")
        return keyed_digest(self._digest_key, _PURPOSE, normalise_invite_code(code))

    def _from_firestore(self, doc_dict: dict, code: Optional[str] = None) -> InviteCode:
        """Convert Firestore document to InviteCode domain entity."""
        # Convert string type back to Enum
        invite_type = InviteType(doc_dict["type"])
        
        return InviteCode(
            code=normalise_invite_code(code) if code else None,
            hint=doc_dict.get("hint"),
            user_id=doc_dict["user_id"],
            account_id=doc_dict["account_id"],
            type=invite_type,
            expires_at=doc_dict["expires_at"],
            created_at=doc_dict["created_at"],
            platform=doc_dict.get("platform"),
            role=doc_dict.get("role", "MEMBER"),
            used_at=doc_dict.get("used_at"),
            used_by_user_id=doc_dict.get("used_by_user_id")
        )

    @log_execution_time
    async def create(self, invite_code: InviteCode) -> InviteCode:
        """Create a new invite code."""
        doc_id = self._doc_id(invite_code.code)
        await self.collection.document(doc_id).set(self._to_firestore(invite_code))

        logger.debug("Created an invite code")
        return invite_code

    @log_execution_time
    async def get_by_code(self, code: str) -> Optional[InviteCode]:
        """Retrieve an invite code by its code string."""
        if not normalise_invite_code(code or ""):
            return None
        doc = await self.collection.document(self._doc_id(code)).get()
        if doc.exists:
            return self._from_firestore(doc.to_dict(), code)
        return None

    @log_execution_time
    async def update(self, invite_code: InviteCode) -> InviteCode:
        """Update an existing invite code."""
        doc_id = self._doc_id(invite_code.code)
        await self.collection.document(doc_id).set(self._to_firestore(invite_code))

        logger.debug("Updated an invite code")
        return invite_code

    @log_execution_time
    async def list_by_user(self, user_id: str) -> List[InviteCode]:
        """List all invite codes created by a specific user."""
        query = self.collection.where(filter=FieldFilter("user_id", "==", user_id))
        docs = query.stream()
        
        results = []
        async for doc in docs:
            results.append(self._from_firestore(doc.to_dict()))
            
        return results
