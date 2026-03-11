#!/bin/bash

# HNSW Vector Index Migration Script
# Migrates development_domain_facts_v2 from FLAT to HNSW indexes
# 
# Usage: ./migrate_to_hnsw_indexes.sh
# 
# Prerequisites:
# - gcloud CLI authenticated with permissions
# - Project: alek-core-427714
# - Database: us-production

set -e

PROJECT_ID="alek-core-427714"
DATABASE_ID="us-production"
CONFIG_FILE="config/firestore.indexes.json"

echo "🚀 HNSW Vector Index Migration"
echo "================================"
echo "Project: $PROJECT_ID"
echo "Database: $DATABASE_ID"
echo "Config: $CONFIG_FILE"
echo ""

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Error: Config file not found: $CONFIG_FILE"
    exit 1
fi

# Check if gcloud is authenticated
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" > /dev/null 2>&1; then
    echo "❌ Error: gcloud not authenticated"
    echo "Run: gcloud auth login"
    exit 1
fi

echo "✅ Config file found"
echo "✅ gcloud authenticated"
echo ""

# Step 1: Apply new HNSW indexes
echo "📝 Step 1: Applying HNSW index configuration..."
echo "This will create new vector indexes with HNSW algorithm"
echo ""

if gcloud firestore indexes composite create \
    --project="$PROJECT_ID" \
    --database="$DATABASE_ID" \
    --field-config="$CONFIG_FILE" 2>&1 | tee /tmp/index_creation.log; then
    
    echo ""
    echo "✅ Index creation initiated successfully"
else
    echo ""
    echo "⚠️  Index creation command failed or requires manual action"
    echo "This may happen if:"
    echo "  1. Indexes already exist (OK - Firestore will update them)"
    echo "  2. Permission denied (requires manual fix in Console)"
    echo ""
    echo "📋 Manual steps if permission denied:"
    echo "  1. Open Firebase Console: https://console.firebase.google.com/project/$PROJECT_ID/firestore/indexes"
    echo "  2. Go to Indexes tab"
    echo "  3. Find development_domain_facts_v2 composite indexes"
    echo "  4. Delete old FLAT indexes (3 total: vector, tags_vector, metadata_vector)"
    echo "  5. Create new HNSW indexes using the config below"
    echo ""
    echo "HNSW Index Config (copy to Console):"
    echo "───────────────────────────────────"
    cat << 'EOF'
Collection: development_domain_facts_v2
Fields:
  - account_id (Ascending)
  - is_current (Ascending) 
  - vector (Vector search, dimension: 768, HNSW)

Repeat for:
  - tags_vector (instead of vector)
  - metadata_vector (instead of vector)
EOF
    echo "───────────────────────────────────"
fi

echo ""
echo "📊 Step 2: Monitoring index creation..."
echo "Run this command to check status:"
echo ""
echo "  gcloud firestore indexes composite list \\"
echo "    --project=$PROJECT_ID \\"
echo "    --database=$DATABASE_ID \\"
echo "    --format='table(name,state,queryScope)'"
echo ""
echo "Index states:"
echo "  - CREATING: Index is being built (~10-30 minutes)"
echo "  - READY: Index is active and serving queries"
echo "  - ERROR: Index creation failed"
echo ""

echo "✅ Migration initiated!"
echo ""
echo "⏳ Next steps:"
echo "  1. Wait 10-30 minutes for indexes to build"
echo "  2. Check status with command above"
echo "  3. Test vector search performance"
echo "  4. Commit updated config/firestore.indexes.json"
echo ""
echo "📈 Expected improvements:"
echo "  - Query latency: 50-100x faster (O(log n) vs O(n))"
echo "  - 1000+ facts: ~300ms → ~5-10ms per query"
echo ""
