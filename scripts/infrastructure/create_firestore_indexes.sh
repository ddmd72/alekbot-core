#!/bin/bash
# -----------------------------------------------------------------------------
# Script: create_firestore_indexes.sh
# Purpose: Sync Firestore indexes from config/firestore.indexes.json to GCP
#          Supports both Composite and Vector indexes
# -----------------------------------------------------------------------------

PROJECT_ID="$PROJECT_ID"
DATABASE="us-production"
CONFIG_FILE="config/firestore.indexes.json"

echo "🔥 Reading index configuration from $CONFIG_FILE..."

# Check if jq is installed
if ! command -v jq &> /dev/null; then
    echo "❌ Error: jq is required but not installed."
    exit 1
fi

# Iterate through each index definition
jq -c '.[]' "$CONFIG_FILE" | while read -r index; do
    COLLECTION=$(echo "$index" | jq -r '.collectionGroup')
    FIELDS=$(echo "$index" | jq -c '.fields')
    
    echo "---------------------------------------------------"
    echo "Processing index for collection: $COLLECTION"
    
    # Build the gcloud command arguments
    ARGS=""
    IS_VECTOR=false
    
    # Process each field
    while read -r field; do
        PATH=$(echo "$field" | jq -r '.fieldPath')
        ORDER=$(echo "$field" | jq -r '.order // empty')
        VECTOR_CONFIG=$(echo "$field" | jq -r '.vectorConfig // empty')
        
        if [[ -n "$VECTOR_CONFIG" ]]; then
            IS_VECTOR=true
            DIMENSION=$(echo "$VECTOR_CONFIG" | jq -r '.dimension')
            ARGS="$ARGS --field-config field-path=$PATH,vector-config={\"dimension\":$DIMENSION,\"flat\":{}}"
        elif [[ "$ORDER" == "ASCENDING" ]]; then
            ARGS="$ARGS --field-config field-path=$PATH,order=ascending"
        elif [[ "$ORDER" == "DESCENDING" ]]; then
            ARGS="$ARGS --field-config field-path=$PATH,order=descending"
        fi
    done < <(echo "$index" | jq -c '.fields[]')
    
    # Execute creation command
    echo "🚀 Creating index..."
    CMD="gcloud firestore indexes composite create --project=$PROJECT_ID --database=$DATABASE --collection-group=$COLLECTION $ARGS"
    
    echo "Executing: $CMD"
    eval $CMD
    
    if [ $? -eq 0 ]; then
        echo "✅ Index creation initiated."
    else
        echo "⚠️ Index creation failed (it might already exist)."
    fi
    
done

echo "---------------------------------------------------"
echo "🎉 All index creation requests submitted."
echo "Use 'gcloud firestore indexes composite list --database=$DATABASE' to check status."
