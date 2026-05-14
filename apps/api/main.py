from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(
    level=os.environ.get("MERGEGUARD_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: ``KEY=VALUE`` lines, no overwrite of existing env."""
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


_load_dotenv(REPO_ROOT / ".env")

from apps.api.webhook_handler import handle_github_webhook
from packages.mongo import LocalMergeGuardStore
from packages.orchestration.engine import AGENT_CATALOG, AGENT_SEQUENCE, MergeGuardOrchestrator
from packages.github_pr import normalize_github_pr_payload


STORE_PATH = REPO_ROOT / "data/agentic_mergeguard.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class MergeGuardHandler(BaseHTTPRequestHandler):
    server_version = "MergeGuardAgenticDemo/0.2"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/api/queue":
            store = self.store()
            self.send_json({"queue": store.queue(), "metrics": store.metrics()})
        elif path == "/api/agents":
            self.send_json({"agents": AGENT_CATALOG, "sequence": AGENT_SEQUENCE})
        elif path.startswith("/api/runs/") and path.endswith("/events"):
            run_id = path.rsplit("/", 2)[-2]
            self._stream_run_events(run_id)
        elif path.startswith("/api/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            run = self.store().get_run(run_id)
            self.send_json(
                {"run": run} if run else {"error": "not found"},
                status=200 if run else 404,
            )
        elif path == "/api/metrics":
            self.send_json(self.store().metrics())
        else:
            self.send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/webhooks/github":
            self.handle_github_webhook_request()
            return
        if path.startswith("/api/runs/") and path.endswith("/apply-actions"):
            self.handle_apply_actions_request(path)
            return
        if path == "/api/demo/analyze":
            body = self.read_json(default={})
            fixture = body.get("fixture", "fixtures/agentic/demo_pr.json")
            payload = json.loads((REPO_ROOT / fixture).read_text())
            store = self.store()
            run = MergeGuardOrchestrator(REPO_ROOT, store).analyze_demo_pr(
                payload,
                enabled_agents=body.get("enabled_agents"),
                agent_delay_ms=int(body.get("agent_delay_ms") or 0),
            )
            self.send_json({"run": run}, status=500 if run.get("state") == "failed" else 200)
        elif path == "/api/github/pr/analyze":
            try:
                body = self.read_json(default={})
                payload = normalize_github_pr_payload(body)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            store = self.store()
            settings = body.get("mergeguard") if isinstance(body.get("mergeguard"), dict) else {}
            run = MergeGuardOrchestrator(REPO_ROOT, store).analyze_pull_request(
                payload,
                enabled_agents=settings.get("enabled_agents"),
                agent_delay_ms=int(settings.get("agent_delay_ms") or 0),
            )
            self.send_json({"run": run}, status=500 if run.get("state") == "failed" else 200)
        elif path.startswith("/api/prs/") and path.endswith("/analyze"):
            body = self.read_json(default={})
            parts = path.strip("/").split("/")
            if len(parts) != 4:
                self.send_json({"error": "expected /api/prs/{pr_id}/analyze"}, status=400)
                return
            pr_id = parts[2]
            store = self.store()
            pr = store.get_pull_request(pr_id)
            if not pr:
                self.send_json({"error": "pull request not found"}, status=404)
                return
            payload = store.latest_input_payload_for_pr(pr_id) or fallback_payload_for_pr(pr)

            run_holder: dict = {}
            run_ready = threading.Event()
            enabled_agents = body.get("enabled_agents")
            agent_delay_ms = int(body.get("agent_delay_ms") or 0)

            def _execute() -> None:
                # Background thread owns its own store instance so concurrent
                # save() calls from the SSE reader's per-request store cannot
                # interleave through a shared in-memory state dict.
                bg_store = LocalMergeGuardStore(STORE_PATH)
                bg_store.load()

                def _on_run_created(run_dict: dict) -> None:
                    run_holder.update(run_dict)
                    run_ready.set()

                try:
                    MergeGuardOrchestrator(REPO_ROOT, bg_store).analyze_pull_request(
                        payload,
                        enabled_agents=enabled_agents,
                        agent_delay_ms=agent_delay_ms,
                        on_run_created=_on_run_created,
                    )
                except Exception as exc:  # noqa: BLE001 - ensure run is marked failed
                    if run_holder.get("id"):
                        try:
                            bg_store.fail_run(
                                run_holder["id"], f"{type(exc).__name__}: {exc}"
                            )
                        except Exception:
                            pass
                    logging.exception("background analyze failed")
                finally:
                    # Make absolutely sure the HTTP request doesn't hang on
                    # run_ready.wait if create_analysis_run blew up early.
                    run_ready.set()

            threading.Thread(target=_execute, daemon=True).start()
            # In cloud mode, orchestrator init (platform client setup) plus the
            # two pre-loop store saves (upsert_pull_request + create_analysis_run)
            # against a multi-MB JSON store can easily push past 5s. Give the
            # background thread a generous head start before we 500 — this is
            # only the time-to-first-event, not the run itself.
            if not run_ready.wait(timeout=60) or not run_holder.get("id"):
                self.send_json(
                    {"error": "run did not start within 60s"}, status=504
                )
                return
            self.send_json(
                {
                    "run_id": run_holder["id"],
                    "state": "running",
                    "run": run_holder,
                },
                status=202,
            )
        elif path.startswith("/api/overrides/"):
            body = self.read_json(default={})
            parts = path.strip("/").split("/")
            if len(parts) != 4:
                self.send_json(
                    {"error": "expected /api/overrides/{run_id}/{finding_id}"},
                    status=400,
                )
                return
            _, _, run_id, finding_id = parts
            item = self.store().record_override(
                run_id=run_id,
                finding_id=finding_id,
                reviewer=body.get("reviewer", "demo-reviewer"),
                reason=body.get("reason", "Demo override"),
            )
            self.send_json({"override": item}, status=201)
        else:
            self.send_json({"error": "not found"}, status=404)

    def handle_github_webhook_request(self) -> None:
        length = int(self.headers.get("content-length", "0") or 0)
        body_bytes = self.rfile.read(length) if length else b""
        request_headers = {
            key.lower(): value for key, value in self.headers.items()
        }
        response = handle_github_webhook(
            repo_root=REPO_ROOT,
            body_bytes=body_bytes,
            headers=request_headers,
            store_path=STORE_PATH,
        )
        self.send_json(response.body, status=response.status)

    def handle_apply_actions_request(self, path: str) -> None:
        """POST /api/runs/<run_id>/apply-actions — push the analyzed run's
        comment + check_run to GitHub on reviewer demand.

        Body: {"decision": "auto" | "approve" | "request_changes" | "comment_only"}
        Default decision is "auto" (use ``summary.status``).
        """
        from dataclasses import asdict
        from packages.github_pr import (
            GitHubAuthError,
            apply_tiered_actions,
            load_app_auth_from_env,
        )

        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[3] != "apply-actions":
            self.send_json({"error": "expected /api/runs/{id}/apply-actions"}, status=400)
            return
        run_id = parts[2]

        body = self.read_json(default={})
        decision = str(body.get("decision") or "auto").strip().lower()
        valid_decisions = {"auto", "approve", "request_changes", "comment_only"}
        if decision not in valid_decisions:
            self.send_json(
                {"error": f"invalid decision; pick one of {sorted(valid_decisions)}"},
                status=400,
            )
            return

        store = self.store()
        run = store.get_run(run_id)
        if not run:
            self.send_json({"error": f"run {run_id} not found"}, status=404)
            return
        ctx = run.get("github_context") or {}
        if not ctx.get("repo_full_name") or not ctx.get("head_sha"):
            self.send_json(
                {
                    "error": (
                        "this run has no GitHub context (webhook-sourced runs only); "
                        "demo runs can't be applied to GitHub"
                    ),
                },
                status=400,
            )
            return

        # Resolve a token — installation token (preferred) or PAT fallback.
        token = None
        auth = load_app_auth_from_env()
        installation_id = ctx.get("installation_id")
        if auth and installation_id:
            try:
                token = auth.get_installation_token(int(installation_id)).token
            except GitHubAuthError as e:
                self.send_json({"error": f"github auth failed: {e}"}, status=500)
                return
        if not token:
            token = os.environ.get("GITHUB_TOKEN") or None
        if not token:
            self.send_json(
                {"error": "no GitHub credentials available — set GITHUB_APP_ID + key path or GITHUB_TOKEN"},
                status=500,
            )
            return

        # Pick the effective summary the bundle should use. For overrides we
        # clone the summary and replace ``status`` so the comment markdown
        # already in the run still ships, but the check_run / review fall
        # under the user's chosen verdict.
        summary = dict(run.get("summary") or {})
        analysis_status = summary.get("status", "review")
        if decision == "approve":
            summary["status"] = "pass"
        elif decision == "request_changes":
            summary["status"] = "blocked"
        elif decision == "comment_only":
            summary["status"] = "review"  # neutral check, no REQUEST_CHANGES

        report = apply_tiered_actions(
            ctx["repo_full_name"],
            int(ctx["pr_number"]),
            ctx["head_sha"],
            summary,
            token,
            details_url=ctx.get("details_url"),
        )
        report_dict = asdict(report)
        report_dict["decision_input"] = decision
        report_dict["analysis_status"] = analysis_status
        report_dict["applied_status"] = summary["status"]
        store.record_actions(run_id, report_dict)

        self.send_json(
            {
                "ok": True,
                "run_id": run_id,
                "decision": decision,
                "analysis_status": analysis_status,
                "applied_status": summary["status"],
                "report": report_dict,
            },
            status=200,
        )

    def store(self) -> LocalMergeGuardStore:
        store = LocalMergeGuardStore(STORE_PATH)
        store.load()
        return store

    def read_json(self, default: dict) -> dict:
        length = int(self.headers.get("content-length", "0") or 0)
        if not length:
            return default
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_run_events(self, run_id: str) -> None:
        # Server-Sent Events stream for a single analysis run. Polls the JSON
        # store at 250ms cadence, diffs against the last snapshot, and emits
        # one `agent-status` event per status transition. Terminates with a
        # `run-finished` event when the run reaches `completed` or `failed`.
        try:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "keep-alive")
            self.send_header("x-accel-buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        last_status: dict[str, str] = {}
        last_state: str | None = None
        deadline = time.time() + 600

        try:
            self._sse_send("hello", {"run_id": run_id})
        except (BrokenPipeError, ConnectionResetError):
            return

        while time.time() < deadline:
            try:
                store = LocalMergeGuardStore(STORE_PATH)
                store.load()
                run = store.get_run(run_id)
            except Exception:
                # Half-written JSON or transient I/O; try again on next tick.
                time.sleep(0.25)
                continue

            if not run:
                try:
                    self._sse_send("error", {"message": "run not found"})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            results = run.get("agent_results") or {}
            for agent_id, result in results.items():
                status = str((result or {}).get("status") or "")
                if not status:
                    continue
                if last_status.get(agent_id) == status:
                    continue
                last_status[agent_id] = status
                try:
                    self._sse_send(
                        "agent-status",
                        {
                            "agent_id": agent_id,
                            "status": status,
                            "result": result,
                        },
                    )
                except (BrokenPipeError, ConnectionResetError):
                    return

            run_state = run.get("state")
            if run_state != last_state:
                last_state = run_state
                try:
                    self._sse_send("run-state", {"state": run_state})
                except (BrokenPipeError, ConnectionResetError):
                    return

            if run_state in ("completed", "failed"):
                try:
                    self._sse_send(
                        "run-finished",
                        {"state": run_state, "run_id": run_id},
                    )
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            time.sleep(0.25)

        try:
            self._sse_send("timeout", {"run_id": run_id})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse_send(self, event: str, data: dict) -> None:
        chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()

    def send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    # Allow override via env so the same entrypoint works in a container
    # (bind 0.0.0.0) and in a host shell (bind loopback).
    host = os.environ.get("MERGEGUARD_HOST", "127.0.0.1")
    port = int(os.environ.get("MERGEGUARD_PORT", "4100"))
    server = ThreadingHTTPServer((host, port), MergeGuardHandler)
    print(f"MergeGuard agentic dashboard: http://{host}:{port}")
    server.serve_forever()


def fallback_payload_for_pr(pr: dict) -> dict:
    fixture = REPO_ROOT / "fixtures/agentic/demo_pr.json"
    if (
        pr.get("repository", {}).get("full_name") == "acme/checkout"
        and pr.get("number") == 1842
        and fixture.exists()
    ):
        return json.loads(fixture.read_text())
    return {
        "repository": pr.get("repository", {}),
        "pull_request": {
            "number": pr.get("number"),
            "title": pr.get("title") or f"PR #{pr.get('number')}",
            "body": pr.get("body", ""),
            "author": pr.get("author", "unknown"),
            "base_sha": pr.get("base_sha", "base"),
            "head_sha": pr.get("head_sha", "head"),
            "base_ref": pr.get("base_ref", ""),
            "head_ref": pr.get("head_ref", ""),
            "url": pr.get("url", ""),
            "labels": pr.get("labels", []),
            "issue_refs": pr.get("issue_refs", []),
            "commit_history": pr.get("commit_history", []),
        },
        "changed_files": [],
        "settings": {},
    }


if __name__ == "__main__":
    main()
