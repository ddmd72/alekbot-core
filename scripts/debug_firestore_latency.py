
import asyncio
import time
import os
from google.cloud import firestore
from src.config.settings import load_settings

async def debug_firestore_latency():
    config = load_settings()
    project = config["GOOGLE_CLOUD_PROJECT"]
    print(f"Using project: {project}")
    
    db = firestore.AsyncClient(project=project)
    col = db.collection("development_user_context")
    doc_id = "os.getenv("USER_ID", "DEMO_USER")"
    
    print(f"Attempting to get document {doc_id}...")
    
    import logging
    # logging.basicConfig(level=logging.DEBUG)
    # logging.getLogger("google.auth").setLevel(logging.DEBUG)
    
    for i in range(5):
        start = time.perf_counter()
        doc = await col.document(doc_id).get()
        end = time.perf_counter()
        duration = (end - start) * 1000
        exists = doc.exists
        print(f"Iteration {i+1}: {duration:.2f}ms (exists: {exists})")
        
    print("\nTesting search_facts (vector search)...")
    from src.adapters.firestore_repo import FirestoreFactRepository
    from src.services.embedding_service import EmbeddingService
    
    emb_service = EmbeddingService(api_key=config["GEMINI_API_KEY"])
    repo = FirestoreFactRepository(db, config["ENVIRONMENT_CONFIG"], embedding_service=emb_service)
    
    query_vector = [0.1] * 768 # Dummy vector
    
    for i in range(3):
        start = time.perf_counter()
        results = await repo.search_facts(doc_id, query_vector)
        end = time.perf_counter()
        duration = (end - start) * 1000
        print(f"Search Iteration {i+1}: {duration:.2f}ms (results: {len(results)})")

if __name__ == "__main__":
    asyncio.run(debug_firestore_latency())
