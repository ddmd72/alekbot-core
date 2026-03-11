"""
Localization library for UI messages.
Provides centralized message storage for different languages.
"""
from .uk import UK_MESSAGES, get_message as get_uk_message
from .en import EN_MESSAGES, get_message as get_en_message

__all__ = ['UK_MESSAGES', 'EN_MESSAGES', 'get_uk_message', 'get_en_message']
