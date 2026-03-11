"""
Test: MemorySearchAgent prompt assembled from Firestore + mime_type only (no response_schema).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai import types
from google.cloud import firestore

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.composition.service_container import ServiceContainer
from src.services.prompt_builder import PromptBuilder

QUERY = "What cars do I own?"
MODEL = "gemini-flash-lite-latest"


async def main():
    print(f"Model: {MODEL}")
    print(f"Query: {QUERY!r}\n")

    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]

    database_id = os.getenv("FIRESTORE_DATABASE", "us-production")
    db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"], database=database_id)

    account_repo = FirestoreAccountRepository(
        db_client=db_client,
        collection_name=env_config.account_collection_name,
    )

    container = ServiceContainer(
        config=config,
        db_client=db_client,
        env_config=env_config,
        account_repo=account_repo,
    )

    prompt_builder = PromptBuilder(
        repo=container.repository,
        assembly_service=container.assembly_service,
    )
    system_prompt = await prompt_builder.build_for_agent(
        agent_type="memorysearch",
        user_id=None,
        account_id=None,
        include_biographical=False,
    )

    print(f"=== Assembled system prompt ({len(system_prompt)} chars) ===")
    print(system_prompt[:800])
    print("..." if len(system_prompt) > 800 else "")
    print()

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config_gen = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.0,
        max_output_tokens=200,
        response_mime_type="application/json",
        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
        ],
    )
    contents = [types.Content(role="user", parts=[types.Part(text=f'SEARCH_REQUEST "{QUERY}"')])]

    response = await client.aio.models.generate_content(model=MODEL, contents=contents, config=config_gen)
    candidate = response.candidates[0] if response.candidates else None

    if candidate and candidate.content and candidate.content.parts:
        text = "".join(p.text for p in candidate.content.parts if p.text)
        finish = getattr(candidate, "finish_reason", "?")
        print(f"✅ finish={finish}")
        print(f"Response: {text}")
    else:
        finish = getattr(candidate, "finish_reason", "NO_CANDIDATE") if candidate else "NO_CANDIDATE"
        print(f"❌ EMPTY — finish_reason={finish}")


asyncio.run(main())
