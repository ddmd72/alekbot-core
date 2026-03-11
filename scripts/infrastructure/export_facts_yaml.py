import asyncio
import logging
import argparse
import sys
import os
import yaml
from datetime import datetime
from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.config.settings import load_settings

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class FactsExporter:
    def __init__(self, db: firestore.AsyncClient):
        self.db = db

    async def export_yaml(self, collection_name: str, output_file: str):
        logger.info(f"🚀 Exporting {collection_name} to {output_file}...")
        
        col_ref = self.db.collection(collection_name)
        docs = col_ref.stream()
        
        data = []
        count = 0
        
        async for doc in docs:
            doc_data = doc.to_dict()
            doc_data['id'] = doc.id
            
            # Remove heavy vectors
            if 'vector' in doc_data:
                del doc_data['vector']

            # Serialize special types
            for key, value in doc_data.items():
                if hasattr(value, 'isoformat'):
                    doc_data[key] = value.isoformat()
            
            data.append(doc_data)
            count += 1
            if count % 100 == 0:
                logger.info(f"   Processed {count} docs...")

        # Ensure directory exists
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
            
        logger.info(f"✅ Exported {count} documents to {output_file}")

async def main():
    parser = argparse.ArgumentParser(description="Export Firestore facts to YAML")
    parser.add_argument("--collection", type=str, default="facts", help="Collection name")
    args = parser.parse_args()

    config = load_settings()
    
    # Initialize Firestore (Cloud)
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    exporter = FactsExporter(db)
    
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    output_path = f"backups/facts_backup_{timestamp}.yaml"
    
    try:
        await exporter.export_yaml(args.collection, output_path)
    except Exception as e:
        logger.error(f"❌ Export failed: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
