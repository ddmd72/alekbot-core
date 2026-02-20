import asyncio
import os
from google import genai
from google.genai import types
from datetime import datetime, timezone
from src.config.settings import load_settings

# Mock logger
class Logger:
    def info(self, msg): print(f"INFO: {msg}")
    def warning(self, msg): print(f"WARNING: {msg}")

logger = Logger()

async def test_web_search(query: str):
    settings = load_settings()
    api_key = settings["GEMINI_API_KEY"]
    model_name = "gemini-3-flash-preview"
    
    client = genai.Client(api_key=api_key)
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    
    logger.info(f"🔍 Testing Web Search for: '{query}' using model {model_name}")
    
    current_time_str = datetime.now(timezone.utc).strftime('%A, %d %B %Y, %H:%M %Z')
    
    augmented_query = (
        f"Current Date: {current_time_str}\n"
        f"User Query: {query}\n\n"
        "TASK:\n"
        "You are a helpful AI assistant with access to Google Search. "
        "Your goal is to provide a comprehensive, accurate, and up-to-date answer to the user's query. "
        "You MUST use the Google Search tool to find relevant information, even if you think you know the answer, "
        "to ensure the information is current and grounded.\n\n"
        "RESPONSE GUIDELINES:\n"
        "1.  **Prioritize Accuracy:** Ensure all facts are supported by search results.\n"
        "2.  **Be Comprehensive:** Cover relevant aspects of the query, similar to how the official Gemini app would answer.\n"
        "3.  **Synthesize:** Don't just list results; combine them into a coherent answer.\n"
        "4.  **Slack Formatting:** You MUST use the following 'mrkdwn' format strictly:\n"
        "    - Bold: *text* (single asterisks)\n"
        "    - Lists: * Item (asterisk and space)\n"
        "    - Links: <url|text> (if applicable)\n"
        "    - Do NOT use Markdown headers (#) or bold (**).\n"
    )

    config = types.GenerateContentConfig(
        tools=[grounding_tool],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
    )

    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=[types.Content(role='user', parts=[types.Part(text=augmented_query)])],
            config=config
        )

        if response.candidates:
            candidate = response.candidates[0]
            if candidate.content and candidate.content.parts:
                text_parts = [p.text for p in candidate.content.parts if p.text]
                print("\n--- RESPONSE ---\n")
                print("".join(text_parts))
                
                # Check for grounding metadata
                if hasattr(candidate, 'grounding_metadata'):
                    print("\n--- GROUNDING METADATA ---\n")
                    print(candidate.grounding_metadata)
            else:
                print("No content in response.")
        else:
            print("No candidates in response.")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_web_search("What are the latest Gemini models available in January 2026?"))
