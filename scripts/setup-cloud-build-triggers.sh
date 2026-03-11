#!/bin/bash
# Setup Cloud Build triggers for MkDocs documentation deployment
# Creates triggers for both dev (develop branch) and prod (main branch)

set -e

# Configuration
PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-europe-southwest1}"
USER_EMAIL="${USER_EMAIL:-}"
REPO_OWNER="${REPO_OWNER:-}"
REPO_NAME="${REPO_NAME:-alek-core}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 Setting up Cloud Build triggers for documentation deployment${NC}"
echo ""
echo -e "${YELLOW}Project ID: $PROJECT_ID${NC}"
echo -e "${YELLOW}Repository: $REPO_OWNER/$REPO_NAME${NC}"
echo -e "${YELLOW}Region: $REGION${NC}"
echo ""

# Step 1: Delete existing triggers if they exist
echo "🗑️  Checking for existing triggers..."

if gcloud builds triggers describe docs-deploy-dev --project=$PROJECT_ID &>/dev/null; then
    echo "Deleting existing docs-deploy-dev trigger..."
    gcloud builds triggers delete docs-deploy-dev --project=$PROJECT_ID --quiet
fi

if gcloud builds triggers describe docs-deploy-prod --project=$PROJECT_ID &>/dev/null; then
    echo "Deleting existing docs-deploy-prod trigger..."
    gcloud builds triggers delete docs-deploy-prod --project=$PROJECT_ID --quiet
fi

echo ""

# Step 2: Create dev trigger (develop branch → Cloud Run dev)
echo "📦 Creating dev trigger (develop → Cloud Run)..."

gcloud builds triggers create github \
    --name=docs-deploy-dev \
    --description="Deploy documentation to Cloud Run dev on push to develop" \
    --repo-name=$REPO_NAME \
    --repo-owner=$REPO_OWNER \
    --branch-pattern="^develop$" \
    --build-config=cloudbuild-docs-run.yaml \
    --substitutions="_ENV=dev,_REGION=$REGION,_USER_EMAIL=$USER_EMAIL" \
    --project=$PROJECT_ID

echo -e "${GREEN}✅ Dev trigger created${NC}"
echo ""

# Step 3: Create prod trigger (main branch → Cloud Run prod)
echo "📦 Creating prod trigger (main → Cloud Run)..."

gcloud builds triggers create github \
    --name=docs-deploy-prod \
    --description="Deploy documentation to Cloud Run prod on push to main" \
    --repo-name=$REPO_NAME \
    --repo-owner=$REPO_OWNER \
    --branch-pattern="^main$" \
    --build-config=cloudbuild-docs-run.yaml \
    --substitutions="_ENV=prod,_REGION=$REGION,_USER_EMAIL=$USER_EMAIL" \
    --project=$PROJECT_ID

echo -e "${GREEN}✅ Prod trigger created${NC}"
echo ""

# Step 4: Verify triggers
echo "🔍 Verifying triggers..."
echo ""

gcloud builds triggers list --project=$PROJECT_ID --format="table(name,description,github.push.branch,filename,disabled)"

echo ""
echo -e "${GREEN}✅ Cloud Build triggers setup complete!${NC}"
echo ""
echo "📝 What was created:"
echo "  1. docs-deploy-dev  → Deploys to Cloud Run dev on push to develop"
echo "  2. docs-deploy-prod → Deploys to Cloud Run prod on push to main"
echo ""
echo "🧪 Test the triggers:"
echo ""
echo "  # Manually trigger dev build"
echo "  gcloud builds triggers run docs-deploy-dev --branch=develop"
echo ""
echo "  # Or push to develop"
echo "  git push origin develop"
echo ""
echo "📊 View builds:"
echo "  https://console.cloud.google.com/cloud-build/builds?project=$PROJECT_ID"
echo ""
echo "🔗 Deployed services:"
echo "  Dev:  https://console.cloud.google.com/run/detail/$REGION/alek-docs-dev"
echo "  Prod: https://console.cloud.google.com/run/detail/$REGION/alek-docs-prod"
echo ""
