from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from packages.mongo import LocalMergeGuardStore
from packages.orchestration.engine import MergeGuardOrchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MergeGuard agentic demo analysis.")
    parser.add_argument("fixture", nargs="?", default="fixtures/agentic/demo_pr.json")
    parser.add_argument("--store", default="data/agentic_mergeguard.json")
    args = parser.parse_args()

    repo_root = REPO_ROOT
    store = LocalMergeGuardStore(repo_root / args.store)
    store.load()
    payload = json.loads((repo_root / args.fixture).read_text())
    run = MergeGuardOrchestrator(repo_root, store).analyze_demo_pr(payload)
    print(json.dumps({"analysis_run_id": run["id"], "summary": run["summary"]}, indent=2))


if __name__ == "__main__":
    main()
