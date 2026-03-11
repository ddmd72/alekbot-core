from abc import ABC, abstractmethod
from typing import List, Optional
from ..domain.invite_code import InviteCode


class InviteCodeRepository(ABC):
    """
    Repository interface for managing InviteCode entities.
    """

    @abstractmethod
    async def create(self, invite_code: InviteCode) -> InviteCode:
        """
        Create a new invite code.
        
        Args:
            invite_code: The invite code entity to save
            
        Returns:
            The saved invite code
        """
        pass

    @abstractmethod
    async def get_by_code(self, code: str) -> Optional[InviteCode]:
        """
        Retrieve an invite code by its code string.
        
        Args:
            code: The unique code identifier
            
        Returns:
            InviteCode if found, None otherwise
        """
        pass

    @abstractmethod
    async def update(self, invite_code: InviteCode) -> InviteCode:
        """
        Update an existing invite code (e.g. marking as used).
        
        Args:
            invite_code: The updated invite code entity
            
        Returns:
            The updated invite code
        """
        pass

    @abstractmethod
    async def list_by_user(self, user_id: str) -> List[InviteCode]:
        """
        List all invite codes created by a specific user.
        
        Args:
            user_id: The ID of the user who created the codes
            
        Returns:
            List of InviteCode entities
        """
        pass
