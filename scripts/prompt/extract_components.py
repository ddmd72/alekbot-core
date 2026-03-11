"""
Extract prompt components from existing kernels.

Parses kernel_light and kernel from Firestore,
identifies each Groovy block, and creates component facts.

Session: 23 (Prompt Component Architecture Implementation)
RFC: docs/architecture/rfcs/PROMPT_COMPONENT_ARCHITECTURE_RFC.md

Usage:
    python scripts/prompt/extract_components.py --env development --dry-run
    python scripts/prompt/extract_components.py --env development
"""

import asyncio
import argparse
import re
from typing import List, Dict
from datetime import datetime
from google.cloud import firestore

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.domain.prompt import ComponentScope
from src.utils.logger import logger


class ComponentExtractor:
    """Extract Groovy blocks as components from monolithic kernels."""
    
    def __init__(self, db_client: firestore.AsyncClient, env_config: EnvironmentConfig, dry_run: bool = False):
        self.db = db_client
        self.env_config = env_config
        self.dry_run = dry_run
        self.collection = f"{env_config.firestore_collection_prefix}facts"
        
        # Component mapping: block_name -> (scope, order)
        self.component_map = {
            # CLASS_ROOT (Top-level blocks in class Alek)
            "cognitive_process": (ComponentScope.CLASS_ROOT, 1),
            
            # CLASS_PROPERTIES (Inside properties {})
            "archetype": (ComponentScope.CLASS_PROPERTIES, 10),
            "vibe": (ComponentScope.CLASS_PROPERTIES, 11),
            "motto": (ComponentScope.CLASS_PROPERTIES, 12),
            "voice": (ComponentScope.CLASS_PROPERTIES, 13),
            "few_shot_learning": (ComponentScope.CLASS_PROPERTIES, 14),
            "behavior_guide": (ComponentScope.CLASS_PROPERTIES, 20),
            "humor_engine": (ComponentScope.CLASS_PROPERTIES, 30),
            
            # CLASS_POLICIES (Inside policies {})
            "Output_Language_Protocol": (ComponentScope.CLASS_POLICIES, 100),
            "Privacy_Protocol": (ComponentScope.CLASS_POLICIES, 101),
            "No_Open_Loops": (ComponentScope.CLASS_POLICIES, 102),
            "Anti_Guardian_Syndrome": (ComponentScope.CLASS_POLICIES, 103),
            "Witty_Accentuation": (ComponentScope.CLASS_POLICIES, 104),
            "Align_With_Anchors": (ComponentScope.CLASS_POLICIES, 105),
            
            # CLASS_KNOWLEDGE_BASE (Inside knowledge_base {})
            "biographical_context": (ComponentScope.CLASS_KNOWLEDGE_BASE, 10),
            "few_shot_examples": (ComponentScope.CLASS_KNOWLEDGE_BASE, 20),
            
            # CLASS_PROTOCOLS (Inside protocols {})
            "search_memory_protocol": (ComponentScope.CLASS_PROTOCOLS, 200),
            "web_search_protocol": (ComponentScope.CLASS_PROTOCOLS, 210),
            
            # CLASS_RUNTIME_RULES (Inside runtime_rules {})
            "Slack_Formatting_Protocol": (ComponentScope.CLASS_RUNTIME_RULES, 300),
        }
    
    async def load_kernel(self, lineage_id: str) -> str:
        """Load kernel from Firestore."""
        logger.info(f"📦 Loading {lineage_id} from Firestore...")
        
        query = (
            self.db.collection(self.collection)
            .where(filter=firestore.FieldFilter("owner_id", "==", "SYSTEM"))
            .where(filter=firestore.FieldFilter("lineage_id", "==", lineage_id))
            .where(filter=firestore.FieldFilter("is_current", "==", True))
            .limit(1)
        )
        
        docs = [doc async for doc in query.stream()]
        
        if not docs:
            raise ValueError(f"Kernel '{lineage_id}' not found in Firestore")
        
        return docs[0].to_dict().get("text", "")
    
    def extract_block(self, text: str, block_name: str) -> str:
        """
        Extract a specific Groovy block from text.
        
        Handles nested braces correctly.
        """
        # Pattern: block_name { ... }
        pattern = rf'{re.escape(block_name)}\s*\{{'
        
        match = re.search(pattern, text)
        if not match:
            return ""
        
        start = match.end() - 1  # Position of opening brace
        brace_count = 0
        i = start
        
        while i < len(text):
            if text[i] == '{':
                brace_count += 1
            elif text[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    # Found closing brace
                    content = text[start+1:i].strip()
                    return content
            i += 1
        
        return ""
    
    def extract_properties_section(self, kernel: str) -> Dict[str, str]:
        """Extract all blocks inside properties {}."""
        properties_content = self.extract_block(kernel, "properties")
        
        if not properties_content:
            return {}
        
        extracted = {}
        for block_name in ["archetype", "vibe", "motto", "voice", "few_shot_learning", "behavior_guide", "humor_engine"]:
            if block_name in ["archetype", "vibe", "motto", "voice"]:
                # These are simple key: value pairs, not blocks
                pattern = rf'{block_name}:\s*"([^"]+)"'
                match = re.search(pattern, properties_content)
                if match:
                    extracted[block_name] = f'{block_name}: "{match.group(1)}"'
            else:
                # These are nested blocks
                content = self.extract_block(properties_content, block_name)
                if content:
                    extracted[block_name] = f'{block_name} {{\n{content}\n}}'
        
        return extracted
    
    def extract_policies_section(self, kernel: str) -> Dict[str, str]:
        """Extract all rule blocks inside policies {}."""
        policies_content = self.extract_block(kernel, "policies")
        
        if not policies_content:
            return {}
        
        extracted = {}
        # Find all rule blocks
        rule_pattern = r'(@\w+\s+)?rule\s+(\w+)\s*\([^)]*\)\s*\{'
        
        for match in re.finditer(rule_pattern, policies_content):
            annotation = match.group(1) if match.group(1) else ""
            rule_name = match.group(2)
            
            # Extract full rule block
            start_pos = match.start()
            brace_pos = match.end() - 1
            brace_count = 0
            i = brace_pos
            
            while i < len(policies_content):
                if policies_content[i] == '{':
                    brace_count += 1
                elif policies_content[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        rule_block = policies_content[start_pos:i+1]
                        extracted[rule_name] = rule_block
                        break
                i += 1
        
        return extracted
    
    async def extract_components_from_kernel(self, kernel: str, kernel_type: str) -> List[Dict]:
        """Extract all components from kernel."""
        components = []
        
        # 1. Extract cognitive_process (CLASS_ROOT)
        cognitive = self.extract_block(kernel, "cognitive_process")
        if cognitive:
            components.append({
                "id": "cognitive_process",
                "scope": ComponentScope.CLASS_ROOT,
                "content": f"cognitive_process {{\n{cognitive}\n}}",
                "order": 1
            })
        
        # 2. Extract properties section (CLASS_PROPERTIES)
        properties = self.extract_properties_section(kernel)
        for block_name, content in properties.items():
            if block_name in self.component_map:
                scope, order = self.component_map[block_name]
                components.append({
                    "id": block_name,
                    "scope": scope,
                    "content": content,
                    "order": order
                })
        
        # 3. Extract policies section (CLASS_POLICIES)
        policies = self.extract_policies_section(kernel)
        for rule_name, content in policies.items():
            if rule_name in self.component_map:
                scope, order = self.component_map[rule_name]
                components.append({
                    "id": rule_name,
                    "scope": scope,
                    "content": content,
                    "order": order
                })
        
        return components
    
    async def create_component_fact(self, component: Dict) -> None:
        """Create Firestore fact for component."""
        if self.dry_run:
            logger.info(f"[DRY-RUN] Would create component: {component['id']}")
            return
        
        doc_ref = self.db.collection(self.collection).document()
        
        await doc_ref.set({
            "owner_id": "SYSTEM",
            "status": "active",
            "lineage_id": f"prompt_component_{component['id']}",
            "text": component['content'],
            "metadata": {
                "scope": component['scope'].value,
                "order": component['order'],
                "version": "1.0",
                "component_type": "groovy_block",
                "extracted_from": "kernel_migration",
                "extraction_date": firestore.SERVER_TIMESTAMP
            },
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP
        })
        
        logger.info(f"✅ Created component: {component['id']}")
    
    async def run(self):
        """Execute extraction process."""
        logger.info("="*60)
        logger.info("🚀 Starting Component Extraction")
        logger.info(f"Environment: {self.env_config.env.value}")
        logger.info(f"Mode: {'DRY-RUN' if self.dry_run else 'LIVE'}")
        logger.info("="*60)
        
        # Load kernel_light
        logger.info("\n📦 Step 1: Loading kernel_light...")
        kernel_light = await self.load_kernel("kernel_light")
        logger.info(f"✅ Loaded kernel_light ({len(kernel_light)} chars)")
        
        # Extract components from kernel_light
        logger.info("\n🔍 Step 2: Extracting components from kernel_light...")
        components_light = await self.extract_components_from_kernel(kernel_light, "light")
        logger.info(f"✅ Extracted {len(components_light)} components from kernel_light")
        
        # Create component facts
        logger.info("\n💾 Step 3: Creating component facts...")
        for comp in components_light:
            await self.create_component_fact(comp)
        
        logger.info("\n" + "="*60)
        logger.info(f"✅ Extraction complete! Created {len(components_light)} components")
        logger.info("="*60)


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Extract prompt components from kernels")
    parser.add_argument("--env", choices=["development", "production"], required=True)
    parser.add_argument("--dry-run", action="store_true", help="Preview extraction without creating facts")
    
    args = parser.parse_args()
    
    # Set environment variable
    import os
    os.environ["APP_ENV"] = args.env
    
    # Load configuration
    config = load_settings()
    env_config = EnvironmentConfig()
    
    # Initialize Firestore
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    # Run extraction
    extractor = ComponentExtractor(db, env_config, dry_run=args.dry_run)
    await extractor.run()


if __name__ == "__main__":
    asyncio.run(main())
