from enum import Enum

class TaskComplexity(str, Enum):
    SMALL_TALK = "small_talk"
    INFO_SEARCH = "info_search"
    SIMPLE_ANALYTICS = "simple_analytics"
    DEEP_REASONING = "deep_reasoning"
