#!/usr/bin/env python3
"""
POC: Smart Router with LLM-based Classification
Tests routing decisions with latency tracking.
"""

import asyncio
import time
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Any

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from src.adapters.gemini_adapter import GeminiAdapter
from src.config.settings import load_settings
from src.ports.llm_service import Message, MessagePart

# 10 test cases
TEST_CASES = [
    {"id": 1, "query": "Привіт!", "expected": "quick_response_agent", "category": "greeting"},
    {"id": 2, "query": "Ок", "expected": "quick_response_agent", "category": "acknowledgment"},
    {"id": 3, "query": "Какой номер моего авто?", "expected": "quick_response_agent", "category": "single_memory"},
    {"id": 4, "query": "Покажи все мои документы", "expected": "smart_response_agent", "category": "multi_memory"},
    {"id": 5, "query": "Что ты думаешь о Раневской?", "expected": "quick_response_agent", "category": "knowledge"},
    {"id": 6, "query": "Столица Франции?", "expected": "quick_response_agent", "category": "knowledge"},
    {"id": 7, "query": "Погода завтра", "expected": "smart_response_agent", "category": "web_simple"},
    {"id": 8, "query": "Які ресторани поруч?", "expected": "quick_response_agent", "category": "web_simple_implicit"},
    {"id": 9, "query": "Посоветуй концерты на выходных в Валенсии", "expected": "smart_response_agent", "category": "web_complex"},
    {"id": 10, "query": "У меня проблема с проектом", "expected": "smart_response_agent", "category": "ambiguous"},
]

async def run_poc():
    """Run POC with latency tracking."""
    print("🚀 Starting Smart Router POC...")
    
    # 1. Load System Prompt
    prompt_path = "src/agents/prompts/triage_router_v1.groovy"
    with open(prompt_path, 'r') as f:
        system_prompt = f.read()
    
    # 2. Initialize LLM
    config = load_settings()
    llm = GeminiAdapter(api_key=config["GEMINI_API_KEY"])
    model_name = "gemini-3-flash-preview"
    
    results = []
    latencies = []
    
    print(f"📝 Using model: {model_name}")
    print(f"📊 Total test cases: {len(TEST_CASES)}")
    print("-" * 50)
    
    for test_case in TEST_CASES:
        query = test_case["query"]
        expected = test_case["expected"]
        category = test_case["category"]
        
        print(f"🧪 [{test_case['id']}/10] Testing category '{category}': '{query}'")
        
        start_time = time.time()
        try:
            response = await llm.generate_content(
                model_name=model_name,
                system_instruction=system_prompt,
                messages=[Message(role="user", parts=[MessagePart(text=query)])],
                temperature=0.0 # More deterministic for routing
            )
            latency_ms = (time.time() - start_time) * 1000
            latencies.append(latency_ms)
            
            # Parse JSON
            raw_text = response.text.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:-3].strip()
            elif raw_text.startswith("```"):
                raw_text = raw_text[3:-3].strip()
            
            try:
                decision = json.loads(raw_text)
                target = decision.get("target_agent")
                
                # Verdict
                # For web_simple (13, 14) we accept both if logic is sound, 
                # but based on prompt rules web_search = smart_agent.
                # However, restaurants (15) = implicit location = quick.
                is_pass = (target == expected)
                
                print(f"  🎯 Decision: {target} (Conf: {decision.get('confidence')})")
                print(f"  ⏱️ Latency: {latency_ms:.0f}ms")
                print(f"  {'✅ PASS' if is_pass else '❌ FAIL (Expected: ' + expected + ')'}")
                
                results.append({
                    "test_id": test_case["id"],
                    "query": query,
                    "category": category,
                    "expected": expected,
                    "actual": target,
                    "verdict": "PASS" if is_pass else "FAIL",
                    "latency_ms": round(latency_ms, 2),
                    "decision": decision
                })
                
            except json.JSONDecodeError:
                print(f"  ❌ Failed to parse JSON: {raw_text[:100]}...")
                results.append({
                    "test_id": test_case["id"],
                    "query": query,
                    "category": category,
                    "expected": expected,
                    "actual": "ERROR_JSON",
                    "verdict": "ERROR",
                    "latency_ms": round(latency_ms, 2),
                    "raw_response": raw_text
                })
                
        except Exception as e:
            print(f"  ❌ LLM Error: {e}")
            results.append({
                "test_id": test_case["id"],
                "query": query,
                "category": category,
                "expected": expected,
                "actual": "ERROR_LLM",
                "verdict": "ERROR",
                "latency_ms": 0,
                "error": str(e)
            })
        
        print("-" * 30)

    # 3. Generate Summary
    total = len(results)
    passed = sum(1 for r in results if r["verdict"] == "PASS")
    failed = total - passed
    accuracy = (passed / total) * 100 if total > 0 else 0
    
    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    
    summary = {
        "timestamp": datetime.now().isoformat(),
        "model": model_name,
        "total_tests": total,
        "passed": passed,
        "failed": failed,
        "accuracy_percent": round(accuracy, 2),
        "latency_stats": {
            "avg_ms": round(avg_latency, 2),
            "min_ms": round(min(latencies), 2) if latencies else 0,
            "max_ms": round(max(latencies), 2) if latencies else 0,
        },
        "by_category": {}
    }
    
    # Category breakdown
    categories = set(r["category"] for r in results)
    for cat in categories:
        cat_results = [r for r in results if r["category"] == cat]
        cat_passed = sum(1 for r in cat_results if r["verdict"] == "PASS")
        summary["by_category"][cat] = {
            "total": len(cat_results),
            "passed": cat_passed,
            "accuracy": round((cat_passed / len(cat_results)) * 100, 2)
        }

    # 4. Save Reports
    report_dir = "reports/router_poc"
    os.makedirs(report_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%md_%H%M%S")
    json_path = f"{report_dir}/results_{timestamp}.json"
    txt_path = f"{report_dir}/results_{timestamp}.txt"
    
    with open(json_path, 'w') as f:
        json.dump({"summary": summary, "results": results}, f, indent=2, ensure_ascii=False)
        
    with open(txt_path, 'w') as f:
        f.write(f"=== Smart Router POC Report ({summary['timestamp']}) ===\n")
        f.write(f"Model: {summary['model']}\n")
        f.write(f"Accuracy: {summary['accuracy_percent']}%\n")
        f.write(f"Avg Latency: {summary['latency_stats']['avg_ms']}ms\n")
        f.write("-" * 50 + "\n\n")
        
        f.write("Category Summary:\n")
        for cat, stats in summary["by_category"].items():
            f.write(f"  {cat:20}: {stats['accuracy']:>6}% ({stats['passed']}/{stats['total']})\n")
        
        f.write("\n" + "=" * 50 + "\n")
        f.write("Detailed Results:\n")
        for r in results:
            f.write(f"[{r['test_id']}] Query: {r['query']}\n")
            f.write(f"      Expected: {r['expected']}\n")
            f.write(f"      Actual:   {r['actual']}\n")
            f.write(f"      Verdict:  {r['verdict']} ({r['latency_ms']}ms)\n")
            if "decision" in r:
                f.write(f"      Reason:   {r['decision'].get('reasoning')}\n")
            f.write("-" * 30 + "\n")

    print(f"\n✅ POC Complete!")
    print(f"📊 Accuracy: {accuracy:.1f}%")
    print(f"⏱️ Avg Latency: {avg_latency:.0f}ms")
    print(f"📄 Reports saved to: {report_dir}")

if __name__ == "__main__":
    asyncio.run(run_poc())
