import os
import google.generativeai as genai
from dotenv import load_dotenv
from google.cloud import secretmanager

def get_secret(secret_name, project_id):
    """Fetch a secret from Google Cloud Secret Manager."""
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        print(f"⚠️ Failed to fetch secret '{secret_name}': {e}")
        return None

load_dotenv()
api_key = get_secret("GEMINI_API_KEY", os.getenv("GOOGLE_CLOUD_PROJECT"))
genai.configure(api_key=api_key)


print("🔍 Searching for available models...\n")

try:
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(f"- {m.name}")
except Exception as e:
    print(f"❌ Critical access error: {e}")