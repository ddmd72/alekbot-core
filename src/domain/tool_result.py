from pydantic import BaseModel
from typing import Any, Optional
from .messaging import RichContent

class ToolResult(BaseModel):
    """
    Standardized result object for all tool executions.
    
    Attributes:
        success: Whether the tool executed successfully.
        data: The actual result data (if successful).
        structured_data: Optional structured payload for rich responses.
        error_message: Description of the error (if failed).
        retry_allowed: Whether the caller should attempt to retry this operation.
    """
    success: bool
    data: Any = None
    structured_data: Optional[RichContent] = None
    error_message: Optional[str] = None
    retry_allowed: bool = False
