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


@dataclass(frozen=True)
class ReferenceImage:
    """One reference image + its own mime type, for edit()."""
    data: bytes
    mime_type: str


class ImageGenerationPort(ABC):
    """Generate and edit images via an external AI image model."""

    @abstractmethod
    async def generate(
        self, prompt: str, *, aspect_ratio: str = "auto", n: int = 1,
        resolution: str = "1k", quality: str = "medium",
    ) -> list[GeneratedImage]:
        """
        Generate image(s) from a text prompt.

        resolution: "1k" | "2k" (xAI's only two valid values).
        quality: "low" | "medium" (xAI's only two valid values; "medium" matches
                 xAI's own stated default). Pricing is NOT flat — it is tiered by
                 (resolution, quality), live-verified against the real API
                 2026-08-24: $0.04 (1k/low) to $0.08 (2k/medium). See
                 domain/billing.py's _IMAGE_TIER_PRICING_USD for the exact table
                 and how it was confirmed (an earlier "pricing is flat" belief,
                 shipped in the same PR as this port, was wrong).

        Returns [] on failure (mirrors ImageSearchPort's contract) — callers
        must handle an empty list as "no image produced", not raise.
        """

    @abstractmethod
    async def edit(
        self, prompt: str, reference_images: list[ReferenceImage], *,
        resolution: str = "1k", quality: str = "medium",
    ) -> GeneratedImage:
        """
        Edit an existing image per a text instruction, using 1-3 reference images
        (xAI's hard cap). When multiple, xAI addresses them as <IMAGE_0>, <IMAGE_1>,
        <IMAGE_2> in prompt-order — reference_images order is load-bearing.

        resolution/quality: same meaning and pricing as generate() (see above) —
        confirmed live 2026-08-24 that xAI's /v1/images/edits accepts both
        fields too (an earlier RFC decision assumed edit had no such fields at
        all; that was wrong, corrected against the live REST API schema).

        Raises on failure (single-result contract, no partial-success shape).
        """
