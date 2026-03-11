"""
GeminiToolFactory — creates Gemini-specific grounding tools.

Encapsulates google.genai.types imports so that composition/ does not
depend directly on the Google GenAI SDK for tool creation.
"""
from google.genai import types


def create_google_search_tool() -> types.Tool:
    """Create a Google Search grounding tool for WebSearchAgent / WebSearchLightAgent."""
    return types.Tool(google_search=types.GoogleSearch())


def create_google_maps_tool(enable_widget: bool = True) -> types.Tool:
    """Create a Google Maps grounding tool for MapsSearchAgent."""
    return types.Tool(google_maps=types.GoogleMaps(enable_widget=enable_widget))
