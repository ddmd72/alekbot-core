"""
Fix Component Braces
====================

Download, validate, fix, and upload prompt components.

Usage:
    python scripts/prompt/fix_component_braces.py --agent smart --action download
    python scripts/prompt/fix_component_braces.py --agent smart --action validate
    python scripts/prompt/fix_component_braces.py --agent smart --action fix
    python scripts/prompt/fix_component_braces.py --agent smart --action upload
    python scripts/prompt/fix_component_braces.py --agent smart --action all
"""

import asyncio
import argparse
import sys
import os
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig


def count_braces(text: str) -> tuple[int, int]:
    """Count opening and closing braces."""
    return text.count('{'), text.count('}')


def validate_component(component_id: str, text: str) -> dict:
    """Validate component syntax."""
    open_count, close_count = count_braces(text)
    balanced = open_count == close_count
    
    return {
        "component_id": component_id,
        "open_braces": open_count,
        "close_braces": close_count,
        "balanced": balanced,
        "diff": close_count - open_count,
        "text_length": len(text)
    }


async def download_components(agent_type: str, output_dir: str):
    """Download all AGENT-level components for specified agent."""
    config = load_settings()
    env_config = EnvironmentConfig()
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    collection_name = f"{env_config.firestore_collection_prefix}prompt_components"
    collection = db.collection(collection_name)
    
    print(f"📥 Downloading components for agent={agent_type}")
    print(f"   Collection: {collection_name}")
    print(f"   Owner: AGENT/{agent_type}")
    print("=" * 70)
    
    # Query for AGENT-level components
    query = collection.where(
        filter=firestore.FieldFilter("owner_type", "==", "AGENT")
    ).where(
        filter=firestore.FieldFilter("owner_value", "==", agent_type)
    )
    
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    components_found = 0
    async for doc in query.stream():
        data = doc.to_dict()
        component_id = data.get("component_id")
        text = data.get("text", "")
        
        # Save to file
        filename = f"{component_id}.groovy"
        filepath = output_path / filename
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)
        
        # Validate
        validation = validate_component(component_id, text)
        status = "✅" if validation["balanced"] else "❌"
        
        print(f"{status} {component_id}: {validation['open_braces']} open, {validation['close_braces']} close")
        components_found += 1
    
    print("=" * 70)
    print(f"✅ Downloaded {components_found} components to {output_dir}")
    return components_found


async def validate_components(input_dir: str) -> list[dict]:
    """Validate all components in directory."""
    input_path = Path(input_dir)
    
    if not input_path.exists():
        print(f"❌ Directory not found: {input_dir}")
        return []
    
    print(f"🔍 Validating components in {input_dir}")
    print("=" * 70)
    
    results = []
    for filepath in sorted(input_path.glob("*.groovy")):
        component_id = filepath.stem
        text = filepath.read_text(encoding="utf-8")
        
        validation = validate_component(component_id, text)
        results.append(validation)
        
        status = "✅" if validation["balanced"] else "❌"
        if not validation["balanced"]:
            diff = validation["diff"]
            if diff > 0:
                print(f"{status} {component_id}: {diff} EXTRA closing brace(s)")
            else:
                print(f"{status} {component_id}: {abs(diff)} MISSING closing brace(s)")
        else:
            print(f"{status} {component_id}: Balanced ({validation['open_braces']} pairs)")
    
    print("=" * 70)
    balanced = sum(1 for r in results if r["balanced"])
    print(f"✅ {balanced}/{len(results)} components are balanced")
    
    return results


def fix_component(filepath: Path) -> bool:
    """Try to fix component by removing extra closing braces."""
    text = filepath.read_text(encoding="utf-8")
    open_count, close_count = count_braces(text)
    
    if open_count == close_count:
        print(f"  ✅ {filepath.stem}: Already balanced")
        return False
    
    diff = close_count - open_count
    
    if diff > 0:
        # Remove extra closing braces from the end
        print(f"  🔧 {filepath.stem}: Removing {diff} extra closing brace(s)")
        
        # Remove trailing closing braces
        lines = text.splitlines()
        removed = 0
        fixed_lines = []
        
        for line in reversed(lines):
            if removed < diff and line.strip() == '}':
                removed += 1
                continue
            fixed_lines.insert(0, line)
        
        fixed_text = '\n'.join(fixed_lines)
        
        # Verify fix
        new_open, new_close = count_braces(fixed_text)
        if new_open == new_close:
            filepath.write_text(fixed_text, encoding="utf-8")
            print(f"  ✅ {filepath.stem}: Fixed! ({new_open} balanced pairs)")
            return True
        else:
            print(f"  ❌ {filepath.stem}: Auto-fix failed (still unbalanced)")
            return False
    else:
        print(f"  ❌ {filepath.stem}: Missing {abs(diff)} closing brace(s) - manual fix needed")
        return False


def fix_components(input_dir: str):
    """Fix all unbalanced components in directory."""
    input_path = Path(input_dir)
    
    if not input_path.exists():
        print(f"❌ Directory not found: {input_dir}")
        return
    
    print(f"🔧 Fixing components in {input_dir}")
    print("=" * 70)
    
    fixed_count = 0
    for filepath in sorted(input_path.glob("*.groovy")):
        if fix_component(filepath):
            fixed_count += 1
    
    print("=" * 70)
    print(f"✅ Fixed {fixed_count} component(s)")


async def upload_components(agent_type: str, input_dir: str):
    """Upload fixed components back to Firestore."""
    input_path = Path(input_dir)
    
    if not input_path.exists():
        print(f"❌ Directory not found: {input_dir}")
        return
    
    config = load_settings()
    env_config = EnvironmentConfig()
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    collection_name = f"{env_config.firestore_collection_prefix}prompt_components"
    collection = db.collection(collection_name)
    
    print(f"📤 Uploading components for agent={agent_type}")
    print(f"   Collection: {collection_name}")
    print(f"   Owner: AGENT/{agent_type}")
    print("=" * 70)
    
    # Confirm before upload
    response = input("⚠️  This will overwrite components in Firestore. Continue? (yes/no): ")
    if response.lower() != "yes":
        print("❌ Upload cancelled")
        return
    
    uploaded_count = 0
    for filepath in sorted(input_path.glob("*.groovy")):
        component_id = filepath.stem
        text = filepath.read_text(encoding="utf-8")
        
        # Validate before upload
        validation = validate_component(component_id, text)
        if not validation["balanced"]:
            print(f"❌ Skipping {component_id}: Still unbalanced!")
            continue
        
        # Find existing document
        query = collection.where(
            filter=firestore.FieldFilter("component_id", "==", component_id)
        ).where(
            filter=firestore.FieldFilter("owner_type", "==", "AGENT")
        ).where(
            filter=firestore.FieldFilter("owner_value", "==", agent_type)
        ).limit(1)
        
        docs = [doc async for doc in query.stream()]
        if not docs:
            print(f"⚠️  Skipping {component_id}: Not found in Firestore")
            continue
        
        doc_ref = docs[0].reference
        await doc_ref.update({"text": text})
        
        print(f"✅ Uploaded {component_id} ({len(text)} chars)")
        uploaded_count += 1
    
    print("=" * 70)
    print(f"✅ Uploaded {uploaded_count} component(s)")


async def main():
    parser = argparse.ArgumentParser(description="Fix Component Braces")
    parser.add_argument(
        "--agent",
        required=True,
        choices=["quick", "smart", "router", "websearch", "consolidation"],
        help="Agent type"
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["download", "validate", "fix", "upload", "all"],
        help="Action to perform"
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Directory for components (default: memory/components/<agent>)"
    )
    
    args = parser.parse_args()
    
    # Default directory
    if args.dir:
        work_dir = args.dir
    else:
        work_dir = f"memory/components/{args.agent}"
    
    try:
        if args.action == "download" or args.action == "all":
            await download_components(args.agent, work_dir)
        
        if args.action == "validate" or args.action == "all":
            await validate_components(work_dir)
        
        if args.action == "fix" or args.action == "all":
            fix_components(work_dir)
            # Re-validate after fix
            if args.action == "all":
                print("\n🔍 Re-validating after fix:")
                await validate_components(work_dir)
        
        if args.action == "upload":
            await upload_components(args.agent, work_dir)
        
        if args.action == "all":
            print("\n📤 Ready to upload. Run with --action upload to proceed.")
            
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
