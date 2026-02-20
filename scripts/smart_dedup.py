#!/usr/bin/env python3
"""
Smart deduplication using LLM to analyze each group and select best fact
Processes all remaining groups automatically with quality analysis
"""

import asyncio
import os
from typing import List, Dict, Tuple
from google import genai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Gemini
api_key = os.getenv('GEMINI_API_KEY')
if not api_key:
    raise ValueError("GEMINI_API_KEY not found in .env")
client = genai.Client(api_key=api_key)

SYSTEM_PROMPT = """You are a fact deduplication expert. Analyze two similar facts and determine which one to KEEP.

CRITERIA (Priority order):
1. **Completeness**: More details, context, measurements
2. **Specificity**: Dates, locations, exact values
3. **Clarity**: Professional medical terminology
4. **Type**: state > event for static facts

OUTPUT FORMAT (JSON only):
{
  "keep": "A" or "B",
  "reason": "Brief explanation (max 15 words)"
}

EXAMPLES:

Fact A: "Weight was 83 kg in March 2025"
Fact B: "Weight was approximately 83 kg (Puzol, Spain) in March 2025, representing a 15 kg loss"
→ {"keep": "B", "reason": "Includes location and weight loss context"}

Fact A: "The patient's HbA1c was 5.1%, indicating no diabetes"
Fact B: "HbA1c was 5.1%"
→ {"keep": "A", "reason": "Includes medical interpretation"}
"""

async def analyze_duplicate(fact_a: str, fact_b: str) -> Tuple[str, str]:
    """Use LLM to analyze which fact to keep"""
    
    prompt = f"""Analyze these duplicate facts:

**Fact A:** {fact_a}
**Fact B:** {fact_b}

Which fact should we KEEP? Output JSON only."""

    try:
        response = await client.aio.models.generate_content(
            model='gemini-3-flash-preview',
            contents=prompt,
            config={
                'system_instruction': SYSTEM_PROMPT,
                'temperature': 0.1,
                'response_mime_type': 'application/json'
            }
        )
        
        import json
        result = json.loads(response.text)
        return result['keep'], result['reason']
    except Exception as e:
        print(f"⚠️ LLM error: {e}, defaulting to Fact A")
        return "A", "LLM error - default"

def parse_suspect_groups(filepath: str) -> List[Dict]:
    """Parse suspect groups file"""
    groups = []
    current_group = None
    
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    for line in lines:
        if line.startswith('## Group '):
            if current_group:
                groups.append(current_group)
            # Extract group number and similarity
            parts = line.split('(Similarity: ')
            group_num = int(parts[0].replace('## Group ', '').strip())
            similarity = float(parts[1].rstrip(')\n'))
            current_group = {
                'group': group_num,
                'similarity': similarity,
                'fact_a': None,
                'fact_b': None
            }
        elif line.startswith('**Fact A:**'):
            current_group['fact_a'] = line.replace('**Fact A:**', '').strip()
        elif line.startswith('**Fact B:**'):
            current_group['fact_b'] = line.replace('**Fact B:**', '').strip()
    
    if current_group:
        groups.append(current_group)
    
    return groups

async def process_groups(start: int, end: int):
    """Process groups from start to end using LLM"""
    
    # Parse suspect groups
    print(f"📖 Reading suspect groups...")
    groups = parse_suspect_groups('reports/account_facts_suspect_groups.md')
    
    # Filter to range
    groups_to_process = [g for g in groups if start <= g['group'] <= end]
    print(f"🔍 Processing groups {start}-{end} ({len(groups_to_process)} groups)...")
    
    # Read current deduplicated file
    with open('reports/account_facts_deduplicated.md', 'r', encoding='utf-8') as f:
        content = f.read()
    
    decisions = []
    facts_removed = 0
    
    for i, group in enumerate(groups_to_process, 1):
        print(f"\n📊 Group {group['group']} (similarity: {group['similarity']:.4f})")
        print(f"   A: {group['fact_a'][:70]}...")
        print(f"   B: {group['fact_b'][:70]}...")
        
        # Get LLM decision
        keep, reason = await analyze_duplicate(group['fact_a'], group['fact_b'])
        print(f"   ✅ Keep {keep}: {reason}")
        
        # Find and remove fact
        if keep == "B":
            # Remove Fact A
            for line in content.split('\n'):
                if group['fact_a'][10:60] in line:  # Match middle part (skip type marker)
                    content = content.replace(line + '\n', '')
                    facts_removed += 1
                    break
        else:
            # Remove Fact B
            for line in content.split('\n'):
                if group['fact_b'][10:60] in line:
                    content = content.replace(line + '\n', '')
                    facts_removed += 1
                    break
        
        decisions.append({
            'group': group['group'],
            'keep': keep,
            'reason': reason,
            'removed': group['fact_a'] if keep == "B" else group['fact_b']
        })
        
        # Progress indicator every 10 groups
        if i % 10 == 0:
            remaining = len([l for l in content.split('\n') if l.strip().startswith('- **[')])
            print(f"   📈 Progress: {i}/{len(groups_to_process)} groups, {facts_removed} removed, {remaining} facts remaining")
    
    # Write updated file
    with open('reports/account_facts_deduplicated.md', 'w', encoding='utf-8') as f:
        f.write(content)
    
    remaining_facts = len([line for line in content.split('\n') if line.strip().startswith('- **[')])
    
    print(f"\n🎉 Batch complete:")
    print(f"   Groups processed: {len(groups_to_process)}")
    print(f"   Facts removed: {facts_removed}")
    print(f"   Facts remaining: {remaining_facts}")
    print(f"   📄 Saved to: reports/account_facts_deduplicated.md")
    
    return decisions

if __name__ == '__main__':
    # Process groups 201-274 (FINAL BATCH)
    print("🚀 Starting LLM-powered deduplication (Groups 201-274 - FINAL)...")
    decisions = asyncio.run(process_groups(201, 274))
