#!/usr/bin/env python3
"""
Create approximate vector index for development_domain_facts_v2 collection.

This script creates a new index without "flat" config, making it approximate (ScaNN-based).
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from google.cloud import firestore
from google.cloud.firestore_admin_v1 import FirestoreAdminClient, CreateIndexRequest
from google.cloud.firestore_admin_v1.types import Index


def create_approximate_vector_index():
    """Create approximate index for tags_vector field."""
    
    project_id = "gen-lang-client-0554950952"
    database_id = "us-production"
    collection_group = "development_domain_facts_v2"
    
    # Initialize admin client
    admin_client = FirestoreAdminClient()
    
    # Build parent path
    parent = f"projects/{project_id}/databases/{database_id}/collectionGroups/{collection_group}"
    
    # Define index
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
                field_path="__name__",
                order=Index.IndexField.Order.ASCENDING
            ),
            Index.IndexField(
                field_path="tags_vector",
                vector_config=Index.IndexField.VectorConfig(
                    dimension=768,
                    flat={}  # TEMPORARY: Restore flat for now
                )
            ),
        ]
    )
    
    # Create index
    print(f"Creating approximate index for {collection_group}...")
    print(f"  Database: {database_id}")
    print(f"  Field: tags_vector (dimension=768, approximate)")
    print(f"  Parent: {parent}")
    
    request = CreateIndexRequest(
        parent=parent,
        index=index
    )
    
    operation = admin_client.create_index(request=request)
    
    print(f"\n✅ Index creation started!")
    print(f"Operation name: {operation.operation.name}")
    print(f"\nWaiting for completion (this may take 3-5 minutes)...")
    
    # Wait for completion
    result = operation.result()
    
    print(f"\n🎉 Index created successfully!")
    print(f"Index name: {result.name}")
    print(f"State: {result.state}")
    
    # Verify no "flat" config
    for field in result.fields:
        if field.field_path == "tags_vector":
            print(f"\nVector config:")
            print(f"  Dimension: {field.vector_config.dimension}")
            if hasattr(field.vector_config, 'flat') and field.vector_config.flat:
                print(f"  ⚠️  WARNING: Has 'flat' config (unexpected)")
            else:
                print(f"  ✅ Approximate search (no 'flat' config)")


if __name__ == "__main__":
    create_approximate_vector_index()
