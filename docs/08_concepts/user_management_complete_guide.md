







```bash


  
  -H "Content-Type: application/json" \
  

# Response:





```





```bash


  
  






  
  
```

---





```python
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


    SELF_LINK = "self_link"      # Link platform to YOUR account
    TEAM_INVITE = "team_invite"  # Invite another OAuth user to YOUR account

@dataclass
class InviteCode:
    """
    Represents a secure, time-limited code for linking identities or joining teams.
    """
    code: str                  # Unique identifier (e.g. "ABC-XYZ-123")
    user_id: str               # The user who created this code (Owner)
    account_id: str            # The account this code is tied to
    type: InviteType           # SELF_LINK or TEAM_INVITE
    platform: Optional[str] = None  # Target platform (only for SELF_LINK)
    role: str = "MEMBER"       # Target role (only for TEAM_INVITE)
    expires_at: datetime       # Expiration timestamp (UTC)
    used_at: Optional[datetime] = None  # When the code was consumed
    
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_valid(self) -> bool:
        """Check if code is unused and not expired."""
        now = datetime.now(timezone.utc)
        return self.used_at is None and now < self.expires_at
```



```python
from abc import ABC, abstractmethod
from typing import List, Optional
from ..domain.invite_code import InviteCode

class InviteCodeRepository(ABC):
    @abstractmethod
    async def create(self, invite_code: InviteCode) -> InviteCode:
        pass

    @abstractmethod
    async def get_by_code(self, code: str) -> Optional[InviteCode]:
        pass

    @abstractmethod
    
        
        pass

    @abstractmethod
    async def list_by_user(self, user_id: str) -> List[InviteCode]:
        pass
```



```python


```



```python
class InviteCodeService:
    
        self.repo = repo
        self.user_repo = user_repo
        self.account_repo = account_repo

    async def generate_self_link(self, user_id: str, account_id: str, platform: str) -> InviteCode:
        return await self._create_code(
            user_id, account_id, InviteType.SELF_LINK, platform=platform
        )

    
        return await self._create_code(
            user_id, account_id, InviteType.TEAM_INVITE, role=role
        )

    async def validate_code(self, code: str) -> InviteCode:
        invite = await self.repo.get_by_code(code)
        
            
        return invite

    
        """
        
        """
        invite = await self.repo.get_by_code(code)

        # Validation
        if invite.type != InviteType.TEAM_INVITE:
            
        if not invite.is_valid():
            

        
        
        new_member = await self.user_repo.get_user(new_member_user_id)
        

        
        new_member.account_id = invite.account_id
        await self.user_repo.update_user(new_member)

        
        
        

        
        

        
        

        
```

---





```python

async def create_invite():
    body = await request.get_json()
    

    
    
    

    
        platform = body.get("platform")
        

    
        role = body.get("role", "MEMBER")
        

    
```



```python

async def join_team():
    body = await request.get_json()
    code = body.get("code")

    try:
        
        
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
```

---





```python

    
    
    

    
    
        
        
            try:
                

                
                
                     
                
                     

                
                
                
                

                
                return owner
            except ValueError:
                pass

    
    
```

---




| :----------------------- | :-------------------------------------------------- | :---------------------------------------------------------------------------------- |





---

 Strategy



```python

    
    
    

    
    

    
    

    # 4. Verify
    
    
    
```
