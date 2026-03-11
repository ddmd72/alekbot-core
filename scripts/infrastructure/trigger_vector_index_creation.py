#!/usr/bin/env python3
"""
Trigger vector index creation by running a vector search query.
Firestore will return an error with a direct link to create the required index.
"""

import os
import sys
from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure

# Set environment
os.environ.setdefault('FIRESTORE_DATABASE', 'us-production')

# Initialize Firestore
db = firestore.Client(database='us-production')

# Collection name
COLLECTION = "development_domain_facts_v2"

print("🚀 Triggering vector index creation...")
print(f"   Database: us-production")
print(f"   Collection: {COLLECTION}")
print()

# Dummy vector for search (768 dimensions)
dummy_vector = [0.1] * 768

try:
    print("Executing vector search query (will fail with index link)...")
    print()
    
    # Try to perform a vector search
    # This will fail and provide a link to create the index
    collection_ref = db.collection(COLLECTION)
    
    query = collection_ref.where("account_id", "==", "dummy_account") \
                          .where("is_current", "==", True)
    
    # Execute find_nearest on vector field
    vector_query = query.find_nearest(
        vector_field="vector",
        query_vector=dummy_vector,
        distance_measure=DistanceMeasure.EUCLIDEAN,
        limit=5
    )
    
    results = vector_query.get()
    
    print("✅ Query succeeded! Index already exists or was created.")
    
except Exception as e:
    error_msg = str(e)
    
    if "index" in error_msg.lower() or "FAILED_PRECONDITION" in error_msg:
        print("❌ Index does not exist (expected)")
        print()
        print("=" * 80)
        print(error_msg)
        print("=" * 80)
        print()
        
        # Extract URL if present
        if "https://console.firebase.google.com" in error_msg or "https://console.cloud.google.com" in error_msg:
            print("✅ Found index creation link in error!")
            print("   Click the link above to create the index.")
        else:
            print("ℹ️  Go to Firebase Console and create the index manually:")
            print("   https://console.firebase.google.com/project/$PROJECT_ID/firestore/databases/us-production/indexes")
            print()
            print("   Index configuration:")
            print("   - Collection: development_domain_facts_v2")
            print("   - Fields:")
            print("     • account_id (Ascending)")
            print("     • is_current (Ascending)")
            print("     • vector (Vector search, 768 dimensions, HNSW)")
        
        print()
        print("ℹ️  Repeat for tags_vector and metadata_vector fields")
        sys.exit(0)
    else:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)
