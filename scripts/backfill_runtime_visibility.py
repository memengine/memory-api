"""Dry-run by default; explicit tenant-scoped batches, never source/provider work."""
import argparse
import json

from api.db.database import build_sync_session_factory
from api.services.runtime_visibility_backfill import backfill_visibility_batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--after-id")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true", help="Commit validated visibility metadata only")
    args = parser.parse_args()
    print(json.dumps(backfill_visibility_batch(build_sync_session_factory(), tenant_id=args.tenant_id,
        after_id=args.after_id, limit=args.limit, apply=args.apply)))


if __name__ == "__main__":
    main()
