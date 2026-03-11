#!/usr/bin/env python3
"""
Create missing vector indexes for domain_facts_v2 in us-production.
Uses direct Firestore query to trigger index creation with automatic link generation.
"""
import sys
from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure

PROJECT_ID = "$PROJECT_ID"
DATABASE_ID = "us-production"
COLLECTION = "domain_facts_v2"

# Dummy vector for triggering index creation
DUMMY_VECTOR = [0.1] * 768

def trigger_index_creation(vector_field: str):
    """Trigger index creation by running a vector query that will fail with index link."""
    print(f"\n{'='*80}")
    print(f"🔍 Triggering index creation for: {vector_field}")
    print(f"{'='*80}")
    
    db = firestore.Client(project=PROJECT_ID, database=DATABASE_ID)
    
    try:
        collection_ref = db.collection(COLLECTION)
        
        # Build query with filters matching our index definition
        query = collection_ref.where("account_id", "==", "dummy_test") \
                              .where("is_current", "==", True)
        
        # Execute vector search (will fail if index doesn't exist)
        vector_query = query.find_nearest(
            vector_field=vector_field,
            query_vector=DUMMY_VECTOR,
            distance_measure=DistanceMeasure.EUCLIDEAN,
            limit=5
        )
        
        results = vector_query.get()
        
        print(f"✅ Index for {vector_field} already exists or query succeeded!")
        return True
        
    except Exception as e:
        error_msg = str(e)
        
        if "FAILED_PRECONDITION" in error_msg or "index" in error_msg.lower():
            print(f"❌ Index for {vector_field} does not exist (expected)")
            print()
            
            # Try to extract and display the index creation link
            if "https://console." in error_msg:
                # Find the URL in the error message
                start_idx = error_msg.find("https://console.")
                if start_idx != -1:
                    # Extract URL (ends at first whitespace or closing bracket)
                    url_end_chars = [' ', ')', ']', '\n']
                    end_idx = len(error_msg)
                    for char in url_end_chars:
                        pos = error_msg.find(char, start_idx)
                        if pos != -1 and pos < end_idx:
                            end_idx = pos
                    
                    url = error_msg[start_idx:end_idx]
                    print(f"🔗 Index creation link:")
                    print(f"   {url}")
                    print()
            
            print(f"Full error message:")
            print(f"{'-'*80}")
            print(error_msg)
            print(f"{'-'*80}")
            return False
            
        else:
            print(f"❌ Unexpected error: {e}")
            return False


def main():
    print("="*80)
    print("🚀 PROD VECTOR INDEX CREATION")
    print("="*80)
    print(f"📦 Project: {PROJECT_ID}")
    print(f"💾 Database: {DATABASE_ID}")
    print(f"📊 Collection: {COLLECTION}")
    print()
    
    # Test each vector field
    vector_fields = ["tags_vector", "metadata_vector"]
    
    results = {}
    for field in vector_fields:
        results[field] = trigger_index_creation(field)
    
    print()
    print("="*80)
    print("📊 SUMMARY")
    print("="*80)
    
    for field, exists in results.items():
        status = "✅ EXISTS" if exists else "❌ NEEDS CREATION"
        print(f"   {field}: {status}")
    
    missing = [f for f, exists in results.items() if not exists]
    
    if missing:
        print()
        print("="*80)
        print("⚠️  ACTION REQUIRED")
        print("="*80)
        print()
        print("Firestore cannot create composite vector indexes programmatically.")
        print("You must create them manually using one of these methods:")
        print()
        print("METHOD 1: Use the links printed above (recommended)")
        print("METHOD 2: Firebase Console:")
        print(f"   https://console.firebase.google.com/project/{PROJECT_ID}/firestore/databases/{DATABASE_ID}/indexes")
        print()
        print("Index configuration for each missing field:")
        print()
        for field in missing:
            print(f"   {field}:")
            print(f"     - Collection: {COLLECTION}")
            print(f"     - Query scope: Collection")
            print(f"     - Fields:")
            print(f"         • account_id (Ascending)")
            print(f"         • is_current (Ascending)")
            print(f"         • {field} (Vector, 768 dimensions)")
            print()
        
        print("⏱️  Index creation takes 5-15 minutes")
        print("    Monitor status with:")
        print(f"    gcloud firestore indexes composite list --database={DATABASE_ID}")
        
        sys.exit(1)
    else:
        print()
        print("✅ All required vector indexes exist!")
        sys.exit(0)


if __name__ == "__main__":
    main()
