#!/usr/bin/env python3
"""
Create HNSW vector indexes for development_domain_facts_v2.
Uses Firestore Admin API to create indexes programmatically.
"""

import asyncio
from google.cloud import firestore_admin_v1
from google.cloud.firestore_admin_v1 import CreateIndexRequest, Index
import sys

PROJECT_ID = "gen-lang-client-0554950952"  # From FIREBASE_PROJECT_ID in .env
DATABASE_ID = "us-production"
COLLECTION_ID = "development_domain_facts_v2"

def create_vector_index(client, vector_field: str):
    """Create a single HNSW vector index."""
    
    parent = f"projects/{PROJECT_ID}/databases/{DATABASE_ID}/collectionGroups/{COLLECTION_ID}"
    
    index = Index(
        query_scope=Index.QueryScope.COLLECTION,
        fields=[
            Index.IndexField(
                field_path="account_id",
                order=Index.IndexField.Order.ASCENDING
            ),
            Index.IndexField(
                field_path="is_current",
                order=Index.IndexField.Order.ASCENDING
            ),
            Index.IndexField(
                field_path=vector_field,
                vector_config=Index.IndexField.VectorConfig(
                    dimension=768
                    # Empty config = HNSW by default in Firestore
                )
            ),
        ],
    )
    
    request = CreateIndexRequest(
        parent=parent,
        index=index,
    )
    
    try:
        operation = client.create_index(request=request)
        print(f"✅ Creating {vector_field} index...")
        print(f"   Operation: {operation.operation.name}")
        return operation
    except Exception as e:
        print(f"❌ Error creating {vector_field} index: {e}")
        return None


def main():
    print(f"🚀 Creating HNSW indexes for {COLLECTION_ID}")
    print(f"   Project: {PROJECT_ID}")
    print(f"   Database: {DATABASE_ID}")
    print()
    
    client = firestore_admin_v1.FirestoreAdminClient()
    
    # Create 3 indexes
    vector_fields = ["vector", "tags_vector", "metadata_vector"]
    operations = []
    
    for field in vector_fields:
        op = create_vector_index(client, field)
        if op:
            operations.append((field, op))
    
    print()
    print(f"✅ Initiated {len(operations)} index creations")
    print()
    print("⏳ Indexes are being built (10-30 minutes)")
    print()
    print("Check status:")
    print(f"   https://console.firebase.google.com/project/{PROJECT_ID}/firestore/databases/{DATABASE_ID}/indexes")
    print()
    print("Or run:")
    print(f"   gcloud firestore indexes composite list --project={PROJECT_ID} --database={DATABASE_ID}")
    

if __name__ == "__main__":
    main()
