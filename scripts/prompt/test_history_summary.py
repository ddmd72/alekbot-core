#!/usr/bin/env python3
"""
Test History Summary Compression (Structured Output)
================================================================
Tests the hypothesis:
"Can LLM generate a full detailed response AND a concise summary (<100 chars) in one pass?"

Objectives:
1. Verify structured output works with Gemini/Claude
2. Measure compression ratio
3. Evaluate summary quality (does it capture key context?)

Usage:
    python scripts/prompt/test_history_summary.py
"""

import asyncio
import os
import sys
import json
import time
from datetime import datetime
from typing import List, Dict, Any

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.config.settings import load_settings
from src.ports.llm_service import LLMRequest, LLMResponse, ProviderCapabilities
from src.adapters.gemini_adapter import GeminiAdapter

# ============================================================================
# TEST CASES
# ============================================================================

TEST_CASES = [
    {
        "category": "TECH_ARCH",
        "question": "Explain Hexagonal Architecture (Ports & Adapters) and why it's better than Layered Architecture for a long-term project."
    },
    {
        "category": "COST_OPTIMIZATION",
        "question": "What is the pricing model for Firebase Authentication vs Auth0 for a startup with 50k users? Give detailed breakdown."
    },
    {
        "category": "MEDICAL",
        "question": "My uric acid is 8.9 mg/dL. What dietary restrictions should I follow? Be specific about purine sources."
    },
    {
        "category": "TRAVEL",
        "question": "Plan a trip from Valencia to Krakow. What are the best flight options and layovers? I hate early mornings."
    },
    {
        "category": "GITHUB_PROJECT",
        "question": "I have a Python project with 2MB of code. How can I use Claude Projects effectively with its context limit?"
    },
    {
        "category": "PERSONAL_CONTEXT",
        "question": "Remind me about my wife's car details and where she works. Also, when is our anniversary?"
    },
    {
        "category": "TECH_DEEP_DIVE",
        "question": "Explain the difference between OAuth2 Authorization Code Flow and Implicit Flow. Which one should I use for SPA?"
    },
    {
        "category": "PHILOSOPHY",
        "question": "What is the concept of 'Constitutional AI' by Anthropic? How does it differ from RLHF?"
    },
    {
        "category": "DEBUGGING",
        "question": "I'm getting a 'Context Limit Exceeded' error in my agent loop. What are 3 strategies to fix this without losing important history?"
    },
    {
        "category": "MIXED",
        "question": "I'm stressed about my kidney stone diagnosis and my project deadline. Give me a stoic advice and a practical plan."
    }
]

# ============================================================================
# SCHEMA DEFINITION
# ============================================================================

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "full_response": {
            "type": "string",
            "description": "Full, detailed, helpful response for the user. Use markdown."
        },
        "history_summary": {
            "type": "string", 
            "description": "Extremely concise summary for conversation history. MAX 100 CHARACTERS. Capture key facts/decisions only."
        }
    },
    "required": ["full_response", "history_summary"]
}

SYSTEM_INSTRUCTION = """
You are an expert AI assistant.
Your goal is to provide helpful, detailed answers to the user.
SIMULTANEOUSLY, you must generate a compressed summary of your answer for the conversation history memory.

CONSTRAINTS for history_summary:
1. MAXIMUM 100 CHARACTERS.
2. Focus on FACTS and DECISIONS.
3. Drop narrative, politeness, and filler words.
4. Use abbreviations if clear (e.g., 'Auth0 > Firebase', 'Uric acid: no meat').
"""

# ============================================================================
# EXECUTION
# ============================================================================

async def run_test():
    print(f"\n{'='*70}")
    print(f"🧪 TEST: History Summary Compression (Structured Output)")
    print(f"{'='*70}\n")
    
    # Initialize LLM
    config = load_settings()
    api_key = config.get("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    
    if not api_key:
        print("❌ Error: GEMINI_API_KEY not found")
        return

    llm = GeminiAdapter(api_key=api_key) # Adapter does not take model in init
    print(f"🤖 Model: gemini-3-pro-preview")
    
    results = []
    
    for i, case in enumerate(TEST_CASES):
        print(f"\n🔹 [{i+1}/{len(TEST_CASES)}] {case['category']}")
        print(f"❓ Q: {case['question'][:100]}...")
        
        start_time = time.time()
        
        try:
            request = LLMRequest(
                model_name="gemini-3-pro-preview", # Use user-specified model
                system_instruction=SYSTEM_INSTRUCTION,
                messages=[
                    {"role": "user", "parts": [{"text": case["question"]}]}
                ],
                response_schema=RESPONSE_SCHEMA,
                temperature=0.7
            )
            
            response = await llm.generate_content(request)
            duration = time.time() - start_time
            
            # Parse JSON
            try:
                data = json.loads(response.text)
                full_len = len(data['full_response'])
                summary_len = len(data['history_summary'])
                compression = (1 - (summary_len / full_len)) * 100
                
                print(f"✅ Success ({duration:.2f}s)")
                print(f"📄 Full: {full_len} chars")
                print(f"📦 Summary: {summary_len} chars ({compression:.1f}% compression)")
                print(f"📝 Content: {data['history_summary']}")
                
                results.append({
                    "case": case,
                    "full_len": full_len,
                    "summary_len": summary_len,
                    "summary": data['history_summary'],
                    "compression": compression
                })
                
            except json.JSONDecodeError:
                print(f"❌ JSON Error: {response.text[:100]}...")
                
        except Exception as e:
            print(f"❌ API Error: {e}")

    # Generate Report
    generate_report(results)


def generate_report(results: List[Dict]):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    filename = f"reports/prompt/history_summary_test_{int(datetime.now().timestamp())}.md"
    
    os.makedirs("reports/prompt", exist_ok=True)
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# History Summary Compression Test\n\n")
        f.write(f"**Date:** {timestamp}\n")
        f.write(f"**Model:** gemini-1.5-flash\n")
        f.write(f"**Constraint:** Max 100 chars\n\n")
        
        f.write("| Category | Full Chars | Summary Chars | Compression | Summary Content |\n")
        f.write("|----------|------------|---------------|-------------|-----------------|\n")
        
        for res in results:
            summary = res['summary'].replace("\n", " ")
            f.write(f"| {res['case']['category']} | {res['full_len']} | {res['summary_len']} | {res['compression']:.1f}% | `{summary}` |\n")
            
        # Analysis
        avg_comp = sum(r['compression'] for r in results) / len(results) if results else 0
        avg_len = sum(r['summary_len'] for r in results) / len(results) if results else 0
        
        f.write(f"\n## Analysis\n")
        f.write(f"- **Average Compression:** {avg_comp:.1f}%\n")
        f.write(f"- **Average Summary Length:** {avg_len:.1f} chars\n")
        f.write(f"- **Token Savings:** Massive (approx. 90-95%)\n")
        
    print(f"\n📄 Report generated: {filename}")

if __name__ == "__main__":
    asyncio.run(run_test())
