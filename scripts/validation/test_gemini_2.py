import os
from google import genai
from google.genai import types
from src.config.settings import load_settings

settings = load_settings()
client = genai.Client(api_key=settings["GEMINI_API_KEY"])
try:
    response = client.models.generate_content(
        model="gemini-3-flash-preview",
        contents="Hi",
        config=types.GenerateContentConfig(
            system_instruction="You are a helpful assistant.",
            temperature=0.9
        )
    )
    print(f"Success: {response.text}")
except Exception as e:
    print(f"Error: {e}")
