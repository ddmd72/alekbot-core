"""
ImageGenerationPort — system boundary for AI image generation/editing services.

Implementations: GrokImageAdapter (src/adapters/grok_image_adapter.py).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratedImage:
    """A single generated or edited image."""
    data: bytes
    mime_type: str


class ImageGenerationPort(ABC):
    """Generate and edit images via an external AI image model."""

    @abstractmethod
    async def generate(
        self, prompt: str, *, aspect_ratio: str = "auto", n: int = 1
    ) -> list[GeneratedImage]:
        """
        Generate image(s) from a text prompt.

        Returns [] on failure (mirrors ImageSearchPort's contract) — callers
        must handle an empty list as "no image produced", not raise.
        """

    @abstractmethod
    async def edit(
        self, prompt: str, reference_images: list[bytes], *, mime_type: str = "image/png"
    ) -> GeneratedImage:
        """
        Edit an existing image per a text instruction.

        Raises on failure (single-result contract, no partial-success shape).
        """
