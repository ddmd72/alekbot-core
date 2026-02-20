#!/bin/bash

# Create FLAT vector indexes using gcloud CLI (alpha feature)
# Note: HNSW is NOT supported in Firestore - only FLAT indexes available

set -e

PROJECT_ID="gen-lang-client-0554950952"
DATABASE_ID="us-production"
COLLECTION="development_domain_facts_v2"

echo "🚀 Creating FLAT vector indexes with gcloud alpha"
echo "   Project: $PROJECT_ID"
echo "   Database: $DATABASE_ID"
echo "   Collection: $COLLECTION"
echo ""

# Create index for 'vector' field (FLAT vector index)
echo "📝 Creating index for 'vector' field..."
gcloud alpha firestore indexes composite create \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=account_id \
  --field-config=order=ASCENDING,field-path=is_current \
  --field-config=vector-config='{"dimension":"768","flat":"{}"}',field-path=vector \
  || echo "⚠️  Index creation failed or already exists"

echo ""

# Create index for 'tags_vector' field
echo "📝 Creating index for 'tags_vector' field..."
gcloud alpha firestore indexes composite create \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=account_id \
  --field-config=order=ASCENDING,field-path=is_current \
  --field-config=vector-config='{"dimension":"768","flat":"{}"}',field-path=tags_vector \
  || echo "⚠️  Index creation failed or already exists"

echo ""

# Create index for 'metadata_vector' field
echo "📝 Creating index for 'metadata_vector' field..."
gcloud alpha firestore indexes composite create \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=account_id \
  --field-config=order=ASCENDING,field-path=is_current \
  --field-config=vector-config='{"dimension":"768","flat":"{}"}',field-path=metadata_vector \
  || echo "⚠️  Index creation failed or already exists"

echo ""
echo "✅ Index creation commands executed!"
echo ""
echo "⏳ Indexes are being built (10-30 minutes)"
echo ""
echo "Check status:"
echo "  gcloud firestore indexes composite list \\"
echo "    --project=$PROJECT_ID \\"
echo "    --database=$DATABASE_ID \\"
echo "    --filter='collectionGroup:$COLLECTION'"
echo ""
