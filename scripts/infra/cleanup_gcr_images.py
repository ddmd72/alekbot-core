#!/usr/bin/env python3
"""
Cleanup script for gcr.io Artifact Registry repository.
Deletes untagged Docker images older than 7 days to prevent unbounded storage growth.

Usage: python3 cleanup_gcr_images.py [--dry-run]
"""

import sys
import json
import subprocess
from datetime import datetime
from argparse import ArgumentParser

PROJECT_ID = "gen-lang-client-0554950952"
REPOSITORY = "gcr.io"
PACKAGE = "alek-bot-dev"
RETENTION_DAYS = 7

def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    args = parser.parse_args()

    dry_run = args.dry_run

    if dry_run:
        print("🔍 DRY RUN MODE — no images will be deleted")
    print(f"📦 Cleaning up untagged images in {PACKAGE} older than {RETENTION_DAYS} days...")
    print()

    # Fetch images via gcloud
    cmd = [
        "gcloud", "artifacts", "docker", "images", "list",
        f"us-docker.pkg.dev/{PROJECT_ID}/{REPOSITORY}/{PACKAGE}",
        f"--project={PROJECT_ID}",
        "--format=json(version,createTime)"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error fetching images: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Filter out the "Listing items" banner
    json_lines = "\n".join([line for line in result.stdout.split("\n") if not line.startswith("Listing")])

    try:
        images = json.loads(json_lines)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}", file=sys.stderr)
        print(f"Output was:\n{json_lines}", file=sys.stderr)
        sys.exit(1)

    from datetime import timezone
    current_date = datetime.now(timezone.utc)
    deleted_count = 0

    for img in images:
        version = img.get("version", "")
        create_time_str = img.get("createTime", "")

        if not version or not create_time_str:
            continue

        # Parse ISO 8601 timestamp
        try:
            create_time = datetime.fromisoformat(create_time_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        age = (current_date - create_time).days

        # Untagged images are in sha256:HASH format
        if version.startswith("sha256:") and age > RETENTION_DAYS:
            full_img = f"us-docker.pkg.dev/{PROJECT_ID}/{REPOSITORY}/{PACKAGE}@{version}"
            short_ver = version[:12]

            if dry_run:
                print(f"  [DRY] Would delete: {short_ver}... ({age} days old)")
            else:
                print(f"  🗑️  Deleting: {short_ver}... ({age} days old)")
                del_cmd = [
                    "gcloud", "artifacts", "docker", "images", "delete",
                    full_img, f"--project={PROJECT_ID}", "--quiet"
                ]
                subprocess.run(del_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deleted_count += 1

    print()
    if dry_run:
        print(f"✅ Dry run complete: would delete {deleted_count} of {len(images)} images")
    else:
        if deleted_count > 0:
            print(f"✅ Cleaned up {deleted_count} old untagged images")
        else:
            print(f"✅ No cleanup needed ({len(images)} recent images remain)")

if __name__ == "__main__":
    main()
