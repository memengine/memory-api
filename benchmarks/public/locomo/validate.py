from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.public.locomo.contract import dataset_diagnostics, load_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the official LoCoMo dataset")
    parser.add_argument("--dataset", type=Path, required=True)
    args = parser.parse_args()
    samples, dataset_sha256 = load_dataset(args.dataset)
    print(
        json.dumps(
            {
                "valid": True,
                "dataset_sha256": dataset_sha256,
                **dataset_diagnostics(samples),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
