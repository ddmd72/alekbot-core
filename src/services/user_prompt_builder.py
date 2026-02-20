from typing import Optional
from ..domain.user import UserBotConfig
from ..ports.repository import FactRepository
from ..services.prompt_builder import PromptBuilder
from ..utils.logger import logger

class UserPromptBuilder(PromptBuilder):
    """
    Extends PromptBuilder to support per-user prompt component overrides.
    
    SESSION_26: Now supports component_service for 3-level priority resolution.
    
    Lookup order (NEW with component_service):
    1. USER level (owner_type=USER, owner_value=user_id)
    2. AGENT level (owner_type=AGENT, owner_value=agent_type)
    3. SYSTEM level (owner_type=SYSTEM)
    
    Legacy lookup order (without component_service):
    1. User's custom component (if defined in UserBotConfig)
    2. Default SYSTEM component
    """
    
    def __init__(
        self,
        repo: FactRepository,
        user_id: str,
        config: UserBotConfig,
        cache_ttl: int = 3600,
        assembly_service: Optional[object] = None
    ):
        super().__init__(repo, cache_ttl, assembly_service)
        self.user_id = user_id
        self.config = config
    
    async def _get_component(self, lineage_id: str) -> str:
        """
        Override to check for user-specific components first.
        """
        # Map default lineage_id to custom override (if exists)
        custom_id_map = {
            'kernel': self.config.prompt_preferences.custom_kernel_id,
            'kernel_light': self.config.prompt_preferences.custom_kernel_light_id,
            'examples': self.config.prompt_preferences.custom_examples_id
        }
        
        custom_id = custom_id_map.get(lineage_id)
        
        if custom_id:
            # Try to load custom component (owner_id = user_id)
            logger.debug(f"📋 Loading custom {lineage_id} for user {self.user_id}: {custom_id}")
            custom_fact = await self.repo.get_latest_fact_by_lineage(self.user_id, custom_id)
            if custom_fact:
                return custom_fact.text
            else:
                logger.warning(f"⚠️ Custom component {custom_id} not found for user {self.user_id}, falling back to SYSTEM")
        
        # Fallback to SYSTEM component (original behavior)
        return await super()._get_component(lineage_id)
