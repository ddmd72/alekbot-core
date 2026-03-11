#!/usr/bin/env python3
"""
Semantic deduplication of exported facts using Gemini embeddings.
"""
import asyncio
import re
import sys
import math
from pathlib import Path
from typing import List, Dict, Tuple
from datetime import datetime
from google import genai

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from config.settings import load_settings

# Configuration
INPUT_FILE = "reports/account_facts_export.md"
OUTPUT_FILE = "reports/account_facts_exact_dedup.md"
SUSPECTS_FILE = "reports/account_facts_suspect_groups.md"
SIMILARITY_THRESHOLD = 0.95  # For grouping suspects (not auto-removal)

# Initialize Gemini Client
settings = load_settings()
GEMINI_API_KEY = settings.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not found in settings")

client = genai.Client(api_key=GEMINI_API_KEY)


def parse_markdown_facts(content: str) -> List[Dict]:
    """Parse facts from markdown file."""
    facts = []
    lines = content.split("\n")
    
    for line in lines:
        # Match pattern: - **[TYPE]** TEXT _(valid_from: DATE)_
        match = re.match(r'^- \*\*\[(\w+)\]\*\* (.+?) _\(valid_from: (.+?)\)_$', line)
        if match:
            fact_type, text, valid_from = match.groups()
            facts.append({
                "type": fact_type,
                "text": text,
                "valid_from": valid_from,
                "original_line": line
            })
    
    return facts


async def get_embedding(text: str) -> List[float]:
    """Get embedding for text using Gemini."""
    result = await asyncio.to_thread(
        client.models.embed_content,
        model="models/gemini-embedding-001",
        contents=text,
        config={
            "task_type": "SEMANTIC_SIMILARITY",
            "output_dimensionality": 768
        }
    )
    return result.embeddings[0].values


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    import math
    
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))
    
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    
    return dot_product / (magnitude1 * magnitude2)


def remove_exact_duplicates(facts: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Stage 1: Remove exact text duplicates (simple string match).
    
    Returns:
        (unique_facts, exact_duplicates_removed)
    """
    print(f"📥 Stage 1: Removing exact text duplicates...")
    seen_texts = {}
    unique = []
    exact_dups = []
    
    for fact in facts:
        text = fact["text"]
        if text not in seen_texts:
            seen_texts[text] = fact
            unique.append(fact)
        else:
            exact_dups.append(fact)
    
    print(f"✅ Exact dedup: {len(unique)} unique, {len(exact_dups)} exact duplicates removed")
    return unique, exact_dups


async def find_suspect_groups(facts: List[Dict]) -> List[List[Dict]]:
    """
    Stage 2: Find groups of facts with high similarity (0.95+) for manual review.
    Does NOT auto-remove, just groups suspects.
    
    Returns:
        List of suspect groups (each group is a list of similar facts)
    """
    print(f"\n📥 Stage 2: Finding suspect groups (similarity >= {SIMILARITY_THRESHOLD})...")
    
    # Generate embeddings
    print("🔄 Generating embeddings...")
    for i, fact in enumerate(facts):
        if i % 50 == 0:
            print(f"  Progress: {i}/{len(facts)}")
        fact["embedding"] = await get_embedding(fact["text"])
    
    print("✅ Embeddings generated")
    
    # Find similar pairs
    print("🔍 Finding similar pairs...")
    suspect_pairs = []
    
    for i in range(len(facts)):
        for j in range(i + 1, len(facts)):
            similarity = cosine_similarity(facts[i]["embedding"], facts[j]["embedding"])
            
            if similarity >= SIMILARITY_THRESHOLD:
                suspect_pairs.append({
                    "fact1": facts[i],
                    "fact2": facts[j],
                    "similarity": similarity
                })
    
    # Group suspects
    print(f"✅ Found {len(suspect_pairs)} suspect pairs")
    
    # Create groups from pairs (simple approach - each pair is a group)
    groups = []
    for pair in suspect_pairs:
        groups.append([pair["fact1"], pair["fact2"], pair["similarity"]])
    
    return groups


async def main():
    """Two-stage deduplication pipeline with manual review step."""
    # Read input file
    print(f"📖 Reading {INPUT_FILE}...")
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Parse facts
    facts = parse_markdown_facts(content)
    print(f"✅ Parsed {len(facts)} facts\n")
    
    # STAGE 1: Remove exact text duplicates
    unique_facts, exact_dups = remove_exact_duplicates(facts)
    
    # Save exact dedup results
    print(f"\n📝 Writing {OUTPUT_FILE}...")
    unique_facts_sorted = sorted(unique_facts, key=lambda f: f["text"].lower())
    
    lines = [
        "# Account Facts - Exact Deduplication",
        "",
        f"**Original:** {len(facts)} facts  ",
        f"**After exact dedup:** {len(unique_facts)} facts  ",
        f"**Exact duplicates removed:** {len(exact_dups)}  ",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "---",
        ""
    ]
    
    for fact in unique_facts_sorted:
        lines.append(fact["original_line"])
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    
    print(f"✅ Saved {OUTPUT_FILE}")
    
    # STAGE 2: Find suspect groups (0.95+ similarity)
    suspect_groups = await find_suspect_groups(unique_facts)
    
    # Save suspect groups for manual review
    if suspect_groups:
        print(f"\n📝 Writing {SUSPECTS_FILE}...")
        suspect_lines = [
            "# Suspect Duplicate Groups (Similarity >= 0.95)",
            "",
            f"**Facts after exact dedup:** {len(unique_facts)}  ",
            f"**Suspect pairs found:** {len(suspect_groups)}  ",
            f"**Threshold:** {SIMILARITY_THRESHOLD}  ",
            f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "**Instructions:** Review each group and decide which facts to keep/remove.",
            "",
            "---",
            ""
        ]
        
        for idx, group in enumerate(suspect_groups, 1):
            fact1, fact2, similarity = group
            suspect_lines.append(f"## Group {idx} (Similarity: {similarity:.4f})")
            suspect_lines.append("")
            suspect_lines.append(f"**Fact A:** {fact1['original_line']}")
            suspect_lines.append(f"**Fact B:** {fact2['original_line']}")
            suspect_lines.append("")
            suspect_lines.append("**Action:** [ ] Keep A  [ ] Keep B  [ ] Keep Both")
            suspect_lines.append("")
            suspect_lines.append("---")
            suspect_lines.append("")
        
        with open(SUSPECTS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(suspect_lines))
        
        print(f"✅ Saved {SUSPECTS_FILE}")
    
    print("\n🎉 Done!")
    print(f"  Facts after exact dedup: {len(unique_facts)}")
    print(f"  Exact duplicates removed: {len(exact_dups)}")
    print(f"  Suspect groups for review: {len(suspect_groups)}")
    print(f"\n📋 Next steps:")
    print(f"  1. Review {SUSPECTS_FILE}")
    print(f"  2. AI will manually deduplicate suspect groups")
    print(f"  3. Final clean file will be created")


if __name__ == "__main__":
    asyncio.run(main())
