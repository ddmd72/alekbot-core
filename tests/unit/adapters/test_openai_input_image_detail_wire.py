"""An image part reaches the Responses API with `detail: "auto"`, the documented default the request contract requires."""
import pytest

from src.adapters.openai_adapter import OpenAIAdapter


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/webp"])
def test_an_image_input_part_carries_the_default_detail(mime):
    part = OpenAIAdapter._file_input_part({"mime_type": mime, "base64": "iVBORw0KGgo="})
    assert part == {"type": "input_image", "image_url": f"data:{mime};base64,iVBORw0KGgo=", "detail": "auto"}
