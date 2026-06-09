"""
English UI messages library (playful tone).
Shared across all adapters (Slack, Telegram, Web, etc.)

Adapters can override specific messages if needed, but 99% will use these.
"""
from typing import Dict, List
from ..domain.ui_messages import StatusType, UIMessage


ENTERTAINMENT_INTROS: List[str] = [
    "Entertain yourself while I dig through the web",
    "While I search — hold an ironic pause",
    "Keep your brain busy while mine searches",
    "A fact break while I google",
    "Relax, it's a search, not a flight to Mars",
    "Hold this short story while I'm on my way",
    "While I rummage — here's your mini-dessert",
]

# Fallback prompts when user sends file without text
FILE_FALLBACK_IMAGE = "What's in this photo?"
FILE_FALLBACK_VIDEO = "What's in this video?"
FILE_FALLBACK_PDF = "Tell me about this document"
FILE_FALLBACK_DOCUMENT = "What's in this file?"
FILE_FALLBACK_GENERIC = "Take a look at this file"

# English messages (central library, playful tone)
EN_MESSAGES: Dict[str, List[str]] = {
    StatusType.THINKING.value: [
        "Contemplating the meaning of existence... and your question",
        "Syncing my neurons... please wait, this hurts",
        "Diving deep into my own mind",
        "Building logical chains... hoping they don't break",
        "Activating cognitive modules at full power",
        "Trying not to overheat from your brilliant ideas",
        "Consulting with my personality core",
        "Gathering my thoughts (they keep running away)"
    ],
    StatusType.SEARCHING_MEMORY.value: [
        "Flipping through your archives... it was here somewhere",
        "Searching your memory... hope it's clean in there",
        "Asking my inner librarian",
        "Diving into the ocean of your facts",
        "Pulling memories from the darkest corners",
        "Conducting inventory of your knowledge",
        "Looking for a needle in your memory haystack"
    ],
    StatusType.SEARCHING_WEB.value: [
        "Venturing into the wild internet... wish me luck",
        "Googling like my life depends on it",
        "Searching for answers in the world wide web",
        "Asking smart people on the net",
        "Scanning digital horizons",
        "Cutting through the jungle of information noise",
        "Hunting for fresh facts online"
    ],
    StatusType.PROCESSING_FILE.value: [
        "Analyzing your files",
        "Breaking down the document to atoms",
        "Studying your attachments",
    ],
    StatusType.ERROR.value: [
        "Oops! My neurons got tangled",
        "Something went wrong... probably Mercury retrograde",
        "An error occurred, but I'll get better (someday)",
        "My internal processor says 'ouch'",
        "Seems like I outsmarted myself",
        "System fell into existential crisis",
        "Error 404: My brain not found"
    ]
}


def get_message(status_type: StatusType, overrides: Dict[str, List[str]] = None) -> List[str]:
    """
    Get English message for status type.
    
    Args:
        status_type: Type of status message
        overrides: Optional dict to override specific status types
        
    Returns:
        List of message variants
        
    Example:
        # Use default English
        messages = get_message(StatusType.THINKING)
        
        # Override specific type
        messages = get_message(
            StatusType.THINKING,
            overrides={StatusType.THINKING.value: ["Custom thinking..."]}
        )
    """
    if overrides and status_type.value in overrides:
        return overrides[status_type.value]
    
    return EN_MESSAGES.get(status_type.value, ["Processing..."])
# Fixed single-string UI messages (see domain.ui_messages.UIMessage)
UI_STRINGS: Dict[str, str] = {
    UIMessage.RESPONSE_READY.value: "✅ Response ready.",
    UIMessage.RESPONSE_TRUNCATED_SUFFIX.value: "\n\n... (response truncated)",
    UIMessage.EMPTY_MODEL_RESPONSE.value: "*(empty response from the model)*",
    UIMessage.UNKNOWN_COMMAND.value: "Unknown command: `{command}`",
    UIMessage.NEW_TOPIC_ACK.value: "New topic. History cleared.",
}
