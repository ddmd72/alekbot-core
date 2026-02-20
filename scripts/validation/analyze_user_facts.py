import argparse
import asyncio
import json
import os
import sys
from typing import Dict, Any, List
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

# Add src to python path
sys.path.append(os.getcwd())

from src.config.settings import load_settings
from src.domain.entities import FactEntity, FactType

async def analyze_user_facts(user_id: str):
    """Analyze all facts for a user to understand anchor distribution."""
    
    # Load config to get project ID and env
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    print(f"\n📊 Analyzing facts for user: {user_id}")
    print(f"🌍 Environment: {env_config.env.value}")
    
    # Initialize Firestore
    if env_config.use_emulator:
        print("🏠 Using Firestore EMULATOR")
        db = firestore.AsyncClient(project="emulator-project")
    else:
        print("☁️ Using Firestore CLOUD")
        db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
        
    prefix = env_config.firestore_collection_prefix
    facts_col = db.collection(f"{prefix}facts")
    print(f"📂 Collection: {prefix}facts\n")
    
    try:
        # Get ALL facts for user (no filters other than owner)
        query = facts_col.where(filter=FieldFilter("owner_id", "==", user_id))
        docs = await query.get()
        
        all_facts = []
        for doc in docs:
            data = doc.to_dict()
            # Add doc ID if not present
            if 'id' not in data:
                data['id'] = doc.id
            all_facts.append(data)
            
        total_count = len(all_facts)
        print(f"📈 Total Facts Found: {total_count}")
        
        if total_count == 0:
            print("❌ No facts found for this user.")
            return

        # --- Analysis Categories ---
        
        # 1. By Type
        principles = [f for f in all_facts if f.get('type') == 'PRINCIPLE']
        states = [f for f in all_facts if f.get('type') == 'STATE']
        events = [f for f in all_facts if f.get('type') == 'EVENT']
        other_types = [f for f in all_facts if f.get('type') not in ['PRINCIPLE', 'STATE', 'EVENT']]
        
        print("\n--- 1. Breakdown by Type ---")
        print(f"• PRINCIPLE: {len(principles)}")
        print(f"• STATE:     {len(states)}")
        print(f"• EVENT:     {len(events)}")
        if other_types:
            print(f"• OTHER:     {len(other_types)} (Types: {set(f.get('type') for f in other_types)})")

        # 2. By 'anchor' tag
        anchors_tag = [f for f in all_facts if 'anchor' in (f.get('tags') or [])]
        print("\n--- 2. Breakdown by 'anchor' tag ---")
        print(f"• Has 'anchor' tag: {len(anchors_tag)}")
        
        # 3. Intersection (What ConsolidationAgent loads)
        # Filter: tags contains "anchor" AND is_current=True
        active_anchors = [f for f in anchors_tag if f.get('is_current', False)]
        print(f"• Active Anchors (loaded by agent): {len(active_anchors)}")
        
        # 4. Anomalies
        # Principles WITHOUT 'anchor' tag
        principles_no_tag = [f for f in principles if 'anchor' not in (f.get('tags') or [])]
        # 'anchor' tag but NOT Principle
        anchors_wrong_type = [f for f in anchors_tag if f.get('type') != 'PRINCIPLE']
        
        print("\n--- 3. Anomalies ---")
        if principles_no_tag:
            print(f"⚠️  {len(principles_no_tag)} Facts with type=PRINCIPLE but MISSING 'anchor' tag:")
            for p in principles_no_tag[:5]:
                print(f"   - [{p['id']}] {p.get('text', '')[:60]}... (Tags: {p.get('tags')})")
            if len(principles_no_tag) > 5: print("   ... and more")
        else:
            print("✅ All PRINCIPLE facts have 'anchor' tag")
            
        if anchors_wrong_type:
            print(f"⚠️  {len(anchors_wrong_type)} Facts with 'anchor' tag but WRONG type:")
            for p in anchors_wrong_type[:5]:
                print(f"   - [{p['id']}] Type={p.get('type')} | {p.get('text', '')[:60]}...")
        else:
            print("✅ All 'anchor' tagged facts are type PRINCIPLE")

        # 5. List of Active Anchors (What is actually loaded)
        print("\n--- 4. Currently Loaded Anchors (Sample) ---")
        if active_anchors:
            for i, a in enumerate(active_anchors):
                print(f"{i+1}. [{a['id']}] {a.get('text', '')}")
        else:
            print("❌ No active anchors found.")
            
        # 6. Check for duplicate content (semantic duplicates)
        print("\n--- 5. Content Duplicates Check ---")
        seen_texts = {}
        duplicates = []
        for f in all_facts:
            text = f.get('text', '').strip()
            if text in seen_texts:
                duplicates.append((f['id'], seen_texts[text], text))
            else:
                seen_texts[text] = f['id']
        
        if duplicates:
            print(f"⚠️  Found {len(duplicates)} exact text duplicates:")
            for new_id, old_id, text in duplicates[:5]:
                print(f"   - '{text[:40]}...' ({new_id} vs {old_id})")
        else:
            print("✅ No exact text duplicates found")

    except Exception as e:
        print(f"\n❌ Error during analysis: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze user facts in Firestore")
    parser.add_argument("--user-id", required=True, help="User ID to analyze")
    
    args = parser.parse_args()
    
    asyncio.run(analyze_user_facts(args.user_id))
