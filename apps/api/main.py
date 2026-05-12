from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from packages.mongo import LocalMergeGuardStore
from packages.orchestration.engine import MergeGuardOrchestrator
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
        elif path.startswith("/api/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            run = self.store().get_run(run_id)
            self.send_json({"run": run} if run else {"error": "not found"}, status=200 if run else 404)
        elif path == "/api/metrics":
            self.send_json(self.store().metrics())
        else:
            self.send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/demo/analyze":
            body = self.read_json(default={})
            fixture = body.get("fixture", "fixtures/agentic/demo_pr.json")
            payload = json.loads((REPO_ROOT / fixture).read_text())
            store = self.store()
            run = MergeGuardOrchestrator(REPO_ROOT, store).analyze_demo_pr(payload)
            self.send_json({"run": run}, status=500 if run.get("state") == "failed" else 200)
        elif path == "/api/github/pr/analyze":
            try:
                payload = normalize_github_pr_payload(self.read_json(default={}))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            store = self.store()
            run = MergeGuardOrchestrator(REPO_ROOT, store).analyze_pull_request(payload)
            self.send_json({"run": run}, status=500 if run.get("state") == "failed" else 200)
        elif path.startswith("/api/overrides/"):
            body = self.read_json(default={})
            parts = path.strip("/").split("/")
            if len(parts) != 4:
                self.send_json({"error": "expected /api/overrides/{run_id}/{finding_id}"}, status=400)
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

    def send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    host = "127.0.0.1"
    port = 4100
    server = ThreadingHTTPServer((host, port), MergeGuardHandler)
    print(f"MergeGuard agentic dashboard: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
