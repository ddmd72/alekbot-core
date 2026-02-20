#!/usr/bin/env python3
"""
Debug script: Check biographical principles loading chain.

Problem: 39 principles in Firestore cache, but not appearing in prompt.
This script traces the entire chain to find where they're lost.
"""

import asyncio
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from google.cloud import firestore
from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter


async def main():
    user_id = os.getenv("USER_ID") or "DEMO_USER"
    account_id = f"account-{user_id}"
    
    print("=" * 80)
    print("🔍 BIOGRAPHICAL PRINCIPLES DEBUG")
    print("=" * 80)
    print(f"Account ID: {account_id}")
    print()
    
    # Initialize Firestore (correct project + database)
    db = firestore.AsyncClient(
        project="gen-lang-client-0554950952",
        database="us-production"
    )
    collection = db.collection("development_user_context")
    
    # Step 1: Load raw data from Firestore
    print("📦 STEP 1: Loading from Firestore")
    print("-" * 80)
    doc = await collection.document(account_id).get()
    
    if not doc.exists:
        print(f"❌ Document not found: {account_id}")
        return
    
    data = doc.to_dict()
    print(f"✅ Document found")
    print(f"   Fields: {list(data.keys())}")
    print()
    
    # Step 2: Check cache structure
    print("📋 STEP 2: Cache Structure")
    print("-" * 80)
    
    facts = data.get("biographical_facts", [])
    principles = data.get("principles", [])
    version = data.get("version", "unknown")
    
    print(f"   Cache version: {version}")
    print(f"   biographical_facts: {len(facts)} items")
    print(f"   principles: {len(principles)} items")
    print()
    
    # Step 3: Analyze principles
    print("🔬 STEP 3: Analyzing Principles")
    print("-" * 80)
    
    if not principles:
        print("❌ NO PRINCIPLES IN CACHE!")
        print("   This is the problem - principles field is empty")
    else:
        print(f"✅ Found {len(principles)} principles")
        print()
        
        # Sample first 3 principles
        print("   First 3 principles:")
        for i, principle in enumerate(principles[:3], 1):
            text = principle.get("text", "")[:60]
            fact_type = principle.get("type", "MISSING")
            tags = principle.get("tags", [])
            print(f"   {i}. type={fact_type}, tags={tags}")
            print(f"      text: {text}...")
        print()
    
    # Step 4: Simulate get_biographical_context_cached()
    print("🔄 STEP 4: Simulating get_biographical_context_cached()")
    print("-" * 80)
    
    # Merge facts + principles (same as in production code)
    combined = facts + principles
    print(f"   Merged list: {len(facts)} facts + {len(principles)} principles = {len(combined)} total")
    print()
    
    # Check types in combined list
    type_counts = {}
    for item in combined:
        fact_type = item.get("type", "UNKNOWN")
        type_counts[fact_type] = type_counts.get(fact_type, 0) + 1
    
    print("   Type distribution in combined list:")
    for fact_type, count in sorted(type_counts.items()):
        print(f"      {fact_type}: {count}")
    print()
    
    # Step 5: Test formatter
    print("🎨 STEP 5: Testing BiographicalFactsFormatter")
    print("-" * 80)
    
    formatter = BiographicalFactsFormatter()
    
    # Group by type (formatter's logic)
    groups = formatter._group_by_type(combined)
    print(f"   Formatter created {len(groups)} groups:")
    for group_key, group_facts in groups.items():
        print(f"      {group_key}: {len(group_facts)} items")
    print()
    
    # Check if PRINCIPLE group exists
    if "PRINCIPLE" in groups:
        principle_group = groups["PRINCIPLE"]
        print(f"   ✅ PRINCIPLE group found with {len(principle_group)} items")
        print("      First 3 items:")
        for i, item in enumerate(principle_group[:3], 1):
            text = item.get("text", "")[:60]
            print(f"      {i}. {text}...")
    else:
        print("   ❌ NO PRINCIPLE GROUP!")
        print("      This means formatter didn't recognize any principles")
        print("      Possible causes:")
        print("      - type field is not 'PRINCIPLE'")
        print("      - type field is missing")
        print("      - text field is empty")
    print()
    
    # Step 6: Format full output
    print("📝 STEP 6: Full Formatted Output")
    print("-" * 80)
    
    formatted = formatter.format(combined)
    
    if not formatted:
        print("   ❌ Formatter returned EMPTY string!")
    else:
        lines = formatted.split('\n')
        print(f"   Formatted output: {len(lines)} lines")
        print()
        print("   Preview (first 30 lines):")
        print("   " + "-" * 76)
        for line in lines[:30]:
            print(f"   {line}")
        if len(lines) > 30:
            print(f"   ... ({len(lines) - 30} more lines)")
        print("   " + "-" * 76)
    print()
    
    # Step 7: Summary
    print("=" * 80)
    print("📊 SUMMARY")
    print("=" * 80)
    
    issues = []
    
    if not principles:
        issues.append("❌ Principles field is EMPTY in cache")
    elif "PRINCIPLE" not in groups:
        issues.append("❌ Principles NOT recognized by formatter")
        issues.append(f"   Check: Do {len(principles)} items have type='PRINCIPLE'?")
    elif len(groups.get("PRINCIPLE", [])) != len(principles):
        issues.append(f"⚠️  Principles count mismatch:")
        issues.append(f"   In cache: {len(principles)}")
        issues.append(f"   Formatted: {len(groups.get('PRINCIPLE', []))}")
    else:
        issues.append(f"✅ All {len(principles)} principles processed correctly")
    
    if issues:
        for issue in issues:
            print(issue)
    else:
        print("✅ No issues found - principles should appear in prompt")
    
    print("=" * 80)
    
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
