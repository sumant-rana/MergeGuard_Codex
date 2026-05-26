"""Manual end-to-end test: pr-history-indexer × amp-dashboard.

Runs the pr-history-indexer agent in local mode against the
``Modernization-Factory/amp-dashboard`` repository and a real local
MongoDB. Caps the scan at 50 PRs so the test stays bounded.

Usage::

    GITHUB_TOKEN=... .venv-test/bin/python scripts/test_pr_indexer_amp_dashboard.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Configure store via env BEFORE importing the agent module so its store
# resolver picks up our local MongoDB.
os.environ.setdefault(
    "MONGODB_URI", "mongodb://localhost:27017/?directConnection=true"
)
os.environ.setdefault("MONGODB_HISTORY_DB", "mergeguard_test_amp")

# Token comes from the caller (env). We never echo it.
token = os.environ.get("GITHUB_TOKEN", "").strip()
if not token:
    raise SystemExit("GITHUB_TOKEN env is required")


def _load_agent_module():
    path = REPO_ROOT / "agents/pr-history-indexer/src/pr_history_indexer/main.py"
    spec = importlib.util.spec_from_file_location("pr_history_indexer_e2e", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load agent at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    module = _load_agent_module()

    payload = {
        "onboarding_run_id": f"onb_amp_{int(time.time())}",
        "repository": {
            "owner": "Modernization-Factory",
            "name": "amp-dashboard",
            "full_name": "Modernization-Factory/amp-dashboard",
            "default_branch": "main",
        },
        "source": {
            "provider": "github",
            "mode": "token",
            "api_base_url": "https://api.github.com",
        },
        "scan": {
            "max_prs": 50,
            "state": "closed",
            "include_files": True,
        },
        "storage": {
            "mode": "local",
            "repo_key": "Modernization-Factory/amp-dashboard",
        },
        "credentials": {"github_token": token},
    }

    print(f"[run] starting agent: onboarding_run_id={payload['onboarding_run_id']}")
    started = time.time()
    result = module.app.invoke(payload)
    duration = time.time() - started

    print(f"[run] agent finished in {duration:.1f}s; status={result.get('status')}")
    output = result.get("output") or {}
    summary = output.get("scan_summary") or {}
    print(f"[run] scan_summary: {summary}")
    if output.get("errors"):
        print(f"[run] errors: {output['errors']}")
    if output.get("warnings"):
        print(f"[run] warnings: {output['warnings'][:5]}")

    # Now validate persistence by reading directly from Mongo.
    from pymongo import MongoClient

    client = MongoClient(os.environ["MONGODB_URI"])
    db = client[os.environ["MONGODB_HISTORY_DB"]]
    repo_key = payload["storage"]["repo_key"]

    counts = {
        "repositories": db.repositories.count_documents({"repo_key": repo_key}),
        "onboarding_runs": db.onboarding_runs.count_documents(
            {"onboarding_run_id": payload["onboarding_run_id"]}
        ),
        "prior_prs": db.prior_prs.count_documents({"repo_key": repo_key}),
        "prior_pr_files": db.prior_pr_files.count_documents({"repo_key": repo_key}),
        "repo_history_signals": db.repo_history_signals.count_documents(
            {"repo_key": repo_key}
        ),
        "memory_records": db.memory_records.count_documents({"repo_key": repo_key}),
    }
    print("\n[validate] Mongo counts:")
    for name, count in counts.items():
        print(f"  {name:25s} {count}")

    # Sample one PR + one file + one memory record so we can eyeball
    # what was actually stored.
    print("\n[validate] sample prior_prs (first 3):")
    for doc in db.prior_prs.find({"repo_key": repo_key}).limit(3):
        print(
            "  - #{number} {title!r:60s} merged_at={merged_at}".format(
                number=doc.get("pr_number"),
                title=(doc.get("title") or "")[:55],
                merged_at=doc.get("merged_at"),
            )
        )

    print("\n[validate] sample prior_pr_files (first 3):")
    for doc in db.prior_pr_files.find({"repo_key": repo_key}).limit(3):
        print(
            "  - pr#{pr} {path} (+{add}/-{rem})".format(
                pr=doc.get("pr_number"),
                path=doc.get("path"),
                add=doc.get("additions"),
                rem=doc.get("deletions"),
            )
        )

    print("\n[validate] history signals — frequently_changed_files (top 5):")
    sig = db.repo_history_signals.find_one({"repo_key": repo_key}) or {}
    for entry in (sig.get("frequently_changed_files") or [])[:5]:
        print(f"  - {entry.get('path')}  count={entry.get('count')}")
    print("\n[validate] history signals — hotspot_paths (top 5):")
    for entry in (sig.get("hotspot_paths") or [])[:5]:
        print(
            f"  - {entry.get('path')}  score={entry.get('score')}"
            f"  reasons={entry.get('reasons')}"
        )
    print("\n[validate] history signals — jira_key_frequency:")
    for entry in (sig.get("jira_key_frequency") or [])[:5]:
        print(f"  - project={entry.get('project')}  count={entry.get('count')}")
    print("\n[validate] history signals — owner_activity (top 3):")
    for entry in (sig.get("owner_activity") or [])[:3]:
        print(
            f"  - {entry.get('owner')}  prs={entry.get('pr_count')}"
            f"  files={entry.get('file_count')}"
        )

    print("\n[validate] sample memory_records (first 3):")
    for doc in db.memory_records.find({"repo_key": repo_key}).limit(3):
        print(
            "  - user_id={uid}  type={t}  label={lbl}  embeddings_written={ew}".format(
                uid=doc.get("user_id"),
                t=doc.get("type"),
                lbl=(doc.get("label") or "")[:70],
                ew=doc.get("embeddings_written"),
            )
        )

    # Repo-scoping invariant: every memory record carries repo_key.
    bad = db.memory_records.count_documents(
        {"repo_key": repo_key, "user_id": {"$ne": repo_key}}
    )
    print(
        f"\n[validate] memory_records with user_id != repo_key (should be 0): {bad}"
    )

    print("\n[done]")


if __name__ == "__main__":
    main()
