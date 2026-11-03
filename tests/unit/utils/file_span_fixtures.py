"""Synthetic file parts and the span-attribute shapes the provider-SDK instrumentation writes (one per SDK)."""
import base64
import json
import struct
import zlib

SENTENCE = "Lukas Weber owns the Q3 budget."


def make_pdf() -> bytes:
    return (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n%" + b"A" * 300 + b"\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def make_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = struct.pack(">I", len(data)) + tag + data
        return body + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 16 for _ in range(16))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def anthropic_messages(data: bytes, kind: str = "document", mime: str = "application/pdf") -> str:
    return json.dumps([{"role": "user", "parts": [
        {"type": kind, "source": {"type": "base64", "media_type": mime, "data": b64(data)}},
        {"type": "text", "content": SENTENCE}]}])


def openai_messages(data: bytes, mime: str = "application/pdf") -> str:
    return json.dumps([{"role": "user", "parts": [
        {"type": "blob", "content": f"data:{mime};base64,{b64(data)}", "modality": "image"},
        {"type": "text", "content": SENTENCE}]}])


def gemini_messages(data: bytes, mime: str = "application/pdf") -> str:
    return json.dumps([{"role": "user", "parts": [
        {"type": "blob", "mime_type": mime, "content": b64(data)},
        {"type": "text", "content": SENTENCE}]}])
