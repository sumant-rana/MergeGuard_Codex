from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.core.models import new_id, utc_now


EMPTY_STATE = {
    "pull_requests": [],
    "analysis_runs": [],
    "agent_executions": [],
    "audit_log": [],
    "reviewer_overrides": [],
    "post_merge_outcomes": [],
}


class LocalMergeGuardStore:
    """JSON-backed Mongo-shaped store for demo mode."""

    def __init__(self, path: str | Path = "data/agentic_mergeguard.json") -> None:
        self.path = Path(path)
        self.state = json.loads(json.dumps(EMPTY_STATE))

    def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            loaded = json.loads(self.path.read_text())
            self.state = {**json.loads(json.dumps(EMPTY_STATE)), **loaded}
        else:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n")

    def upsert_pull_request(self, pr: dict[str, Any]) -> dict[str, Any]:
        repo = pr["repository"]["full_name"]
        # Strip incoming "id" — GitHub webhooks include a numeric id that would
        # clobber our stable string ``pr_<uuid>`` and break dashboard lookups.
        pr = {k: v for k, v in pr.items() if k != "id"}
        existing = next(
            (
                item
                for item in self.state["pull_requests"]
                if item["repository"]["full_name"] == repo and item["number"] == pr["number"]
            ),
            None,
        )
        if existing:
            existing.update(pr)
            existing["updated_at"] = utc_now()
            self.save()
            return existing
        record = {**pr, "id": new_id("pr"), "created_at": utc_now(), "updated_at": utc_now()}
        self.state["pull_requests"].append(record)
        self.save()
        return record

    def create_analysis_run(
        self,
        pr_id: str,
        head_sha: str,
        pull_request: dict[str, Any] | None = None,
        input_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = {
            "id": new_id("run"),
            "pull_request_id": pr_id,
            "pull_request": pull_request or {},
            "input_payload": input_payload or {},
            "head_sha": head_sha,
            "state": "running",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "agent_results": {},
            "summary": {},
        }
        self.state["analysis_runs"].append(run)
        self.save()
        return run

    def record_agent_execution(self, run_id: str, execution: dict[str, Any]) -> None:
        self.state["agent_executions"].append(
            {
                "id": new_id("exec"),
                "analysis_run_id": run_id,
                "created_at": utc_now(),
                **execution,
            }
        )
        run = self.get_run(run_id)
        if run:
            agent_id = execution["agent_id"]
            run["agent_results"][agent_id] = execution["result"]
            run["updated_at"] = utc_now()
        self.save()

    def complete_run(self, run_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        run["state"] = "completed"
        run["summary"] = summary
        run["updated_at"] = utc_now()
        self.state["audit_log"].append(
            {
                "id": new_id("audit"),
                "analysis_run_id": run_id,
                "event": "analysis.completed",
                "summary_status": summary.get("status"),
                "created_at": utc_now(),
            }
        )
        self.save()
        return run

    def fail_run(self, run_id: str, error: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        run["state"] = "failed"
        run["summary"] = {
            "status": "failed",
            "risk_score": 0,
            "top_blocker": "Analysis failed before all agents completed.",
            "next_action": error,
        }
        run["updated_at"] = utc_now()
        self.state["audit_log"].append(
            {
                "id": new_id("audit"),
                "analysis_run_id": run_id,
                "event": "analysis.failed",
                "error": error,
                "created_at": utc_now(),
            }
        )
        self.save()
        return run

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return next((run for run in self.state["analysis_runs"] if run["id"] == run_id), None)

    def set_github_context(self, run_id: str, context: dict[str, Any]) -> None:
        """Stash repo / PR / head-sha / installation_id on the run so the
        manual ``apply-actions`` endpoint can mint a token and post later."""
        run = self.get_run(run_id)
        if not run:
            return
        run["github_context"] = context
        run["updated_at"] = utc_now()
        self.save()

    def record_actions(self, run_id: str, report: dict[str, Any]) -> dict[str, Any] | None:
        """Persist what was actually pushed to GitHub for a run."""
        run = self.get_run(run_id)
        if not run:
            return None
        run["github_actions"] = report
        run["actions_posted_at"] = utc_now()
        run["updated_at"] = utc_now()
        self.state["audit_log"].append(
            {
                "id": new_id("audit"),
                "analysis_run_id": run_id,
                "event": "github.actions.applied",
                "comment_id": report.get("comment_id"),
                "check_run_id": report.get("check_run_id"),
                "review_action": report.get("review_action"),
                "decision_status": report.get("status"),
                "created_at": utc_now(),
            }
        )
        self.save()
        return run

    def latest_run_for_pr(self, pr_id: str) -> dict[str, Any] | None:
        runs = [run for run in self.state["analysis_runs"] if run["pull_request_id"] == pr_id]
        return sorted(runs, key=lambda item: item["created_at"], reverse=True)[0] if runs else None

    def get_pull_request(self, pr_id: str) -> dict[str, Any] | None:
        return next((pr for pr in self.state["pull_requests"] if pr["id"] == pr_id), None)

    def latest_input_payload_for_pr(self, pr_id: str) -> dict[str, Any] | None:
        runs = [
            run
            for run in self.state["analysis_runs"]
            if run["pull_request_id"] == pr_id and run.get("input_payload")
        ]
        if not runs:
            return None
        return sorted(runs, key=lambda item: item["created_at"], reverse=True)[0]["input_payload"]

    def queue(self) -> list[dict[str, Any]]:
        rows = []
        for pr in self.state["pull_requests"]:
            latest = self.latest_run_for_pr(pr["id"])
            rows.append({"pull_request": pr, "latest_run": latest})
        return sorted(
            rows,
            key=lambda row: row.get("latest_run", {})
            .get("summary", {})
            .get("risk_score", 0),
            reverse=True,
        )

    def record_override(
        self,
        run_id: str,
        finding_id: str,
        reviewer: str,
        reason: str,
    ) -> dict[str, Any]:
        item = {
            "id": new_id("override"),
            "analysis_run_id": run_id,
            "finding_id": finding_id,
            "reviewer": reviewer,
            "reason": reason,
            "created_at": utc_now(),
        }
        self.state["reviewer_overrides"].append(item)
        self.save()
        return item

    def metrics(self) -> dict[str, Any]:
        queue = self.queue()
        runs = self.state["analysis_runs"]
        return {
            "generated_at": utc_now(),
            "open_prs": len(queue),
            "analysis_runs": len(runs),
            "completed_runs": len([run for run in runs if run["state"] == "completed"]),
            "high_risk": len(
                [
                    row
                    for row in queue
                    if row.get("latest_run", {}).get("summary", {}).get("risk_score", 0) >= 65
                ]
            ),
            "blocked": len(
                [
                    row
                    for row in queue
                    if row.get("latest_run", {}).get("summary", {}).get("status") == "blocked"
                ]
            ),
            "agent_executions": len(self.state["agent_executions"]),
            "overrides": len(self.state["reviewer_overrides"]),
        }
