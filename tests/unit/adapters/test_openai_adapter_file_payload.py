"""OpenAIAdapter._file_payload: how much base64 attachment data a built request carries."""
from src.adapters.openai_adapter import OpenAIAdapter


def test_counts_image_and_file_parts_and_their_characters():
    items = [
        {"role": "user", "content": "plain string content"},
        {"role": "user", "content": [
            {"type": "input_text", "text": "look at these"},
            {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 100},
            {"type": "input_file", "filename": "a.pdf", "file_data": "data:application/pdf;base64," + "B" * 400},
        ]},
        {"type": "function_call_output", "call_id": "c1", "output": "x"},
    ]
    count, chars = OpenAIAdapter._file_payload(items)
    assert count == 2
    assert chars == len("data:image/png;base64,") + 100 + len("data:application/pdf;base64,") + 400


def test_no_files_means_zero():
    assert OpenAIAdapter._file_payload([{"role": "user", "content": "hi"}]) == (0, 0)
    assert OpenAIAdapter._file_payload([]) == (0, 0)


def test_malformed_parts_are_ignored_not_raised():
    items = [{"role": "user", "content": [None, "x", {"type": "input_image"}, {"type": "input_file", "file_data": 5}]}]
    assert OpenAIAdapter._file_payload(items) == (0, 0)
