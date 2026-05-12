from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

for _repo_root in [*Path(__file__).resolve().parents, Path("/app")]:
    if (_repo_root / "packages").is_dir():
        _repo_root_str = str(_repo_root)
        if _repo_root_str not in sys.path:
            sys.path.insert(0, _repo_root_str)
        break

from packages.agent_runtime import LocalAgentApp, create_app, make_agent_result, register_entrypoint  # noqa: E402
from packages.core.analysis_utils import important_terms, is_docs, is_test, normalize_path  # noqa: E402

AGENT_ID = "semantic-evidence-agent"

app = create_app(
    AGENT_ID,
    "Index and retrieve repository evidence using Magenta memory, Voyage embeddings, and vector search.",
)


@app.tool(is_local=False)
def index_memory_record(record: dict[str, Any], repo_key: str) -> dict[str, Any]:
    """Save one repository evidence record into Magenta semantic memory."""
    memory = getattr(app, "memory", None)
    if memory is None:
        return {"stored": False, "label": record["label"], "reason": "memory unavailable"}
    try:
        stored = memory.save_semantic(
            text=record["text"],
            label=record["label"],
            user_id=repo_key,
            source=AGENT_ID,
            visibility="shared",
            metadata=record.get("metadata", {}),
            upsert=True,
            agent_id=AGENT_ID,
        )
    except TypeError:
        stored = memory.save_semantic(
            text=record["text"],
            label=record["label"],
            user_id=repo_key,
            metadata=record.get("metadata", {}),
        )
    return {"stored": bool(stored), "label": record["label"], "type": record["metadata"]["type"]}


@app.tool(is_local=False)
def search_repository_memory(query: str, repo_key: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Search Magenta semantic memory for repository evidence related to a query."""
    memory = getattr(app, "memory", None)
    if memory is None:
        return []
    try:
        raw_results = memory.search_semantic(query=query, user_id=repo_key, top_k=top_k)
    except TypeError:
        raw_results = memory.search_semantic(query=query, top_k=top_k)
    return [normalize_memory_hit(item) for item in raw_results]


def run(payload: dict[str, Any]) -> dict[str, Any]:
    pr = payload.get("pull_request", {})
    repository = pr.get("repository") or payload.get("repository", {})
    repo_key = repository.get("full_name") or f"{repository.get('owner', 'local')}/{repository.get('name', 'repo')}"
    prior = payload.get("prior_results", {})
    compression = prior.get("review-compression", {}).get("output", {})
    intent = prior.get("intent-extractor", {}).get("output", {})
    semantic = prior.get("semantic-diff-explainer", {}).get("output", {})
    concept = prior.get("concept-classifier", {}).get("output", {})
    files = compression.get("files") or payload.get("changed_files", [])
    intent_items = intent.get("intent_items", [])

    records = memory_records(payload, files, intent_items, semantic, concept)
    indexed = [index_memory_record(record, repo_key) for record in records]
    queries = memory_queries(pr, files, intent_items, semantic)
    query_results = []
    all_hits: list[dict[str, Any]] = []
    for query in queries:
        hits = [
            hit
            for hit in search_repository_memory(
                query["query"],
                repo_key,
                top_k=max(20, int(query.get("top_k", 6)) * 4),
            )
            if is_repository_evidence_hit(hit, payload.get("analysis_run_id", ""))
        ]
        query_results.append(
            {
                **query,
                "hit_count": len(hits),
                "top_labels": [hit["label"] for hit in hits[:4]],
            }
        )
        all_hits.extend(hits)

    semantic_matches = dedupe_hits(all_hits)[:18]
    related_tests = [hit for hit in semantic_matches if hit["kind"] == "test"][:8]
    similar_prs = [hit for hit in semantic_matches if hit["kind"] == "prior_pr"][:6]
    risk_memories = [
        hit
        for hit in semantic_matches
        if hit["kind"] in {"policy", "contract", "runbook"} or hit.get("risk_terms")
    ][:8]
    requirement_evidence = [
        requirement_memory(item, semantic_matches)
        for item in intent_items
        if item.get("category") != "out_of_scope"
    ]
    findings = memory_findings(requirement_evidence, files)
    output = {
        "memory_provider": memory_provider(),
        "platform_capabilities": platform_capabilities(),
        "index": {
            "repo_key": repo_key,
            "records_seen": len(records),
            "records_stored": len([item for item in indexed if item.get("stored")]),
            "source_types": source_type_counts(records),
        },
        "semantic_queries": query_results,
        "semantic_matches": semantic_matches,
        "requirement_evidence": requirement_evidence,
        "related_tests": related_tests,
        "similar_prs": similar_prs,
        "risk_memories": risk_memories,
        "memory_findings": findings,
        "recommended_test_updates": recommended_test_updates(related_tests, requirement_evidence, files),
    }
    return make_agent_result(
        AGENT_ID,
        output,
        confidence=memory_confidence(output),
        messages=[
            (
                f"indexed {output['index']['records_stored']} memory records and "
                f"retrieved {len(semantic_matches)} repository evidence hits"
            )
        ],
        trace=[
            {
                "step": "memory_index",
                "provider": output["memory_provider"],
                "records": len(records),
                "stored": output["index"]["records_stored"],
            },
            {
                "step": "vector_recall",
                "queries": len(query_results),
                "matches": len(semantic_matches),
                "related_tests": len(related_tests),
            },
        ],
    )


def memory_records(
    payload: dict[str, Any],
    files: list[dict[str, Any]],
    intent_items: list[dict[str, Any]],
    semantic: dict[str, Any],
    concept: dict[str, Any],
) -> list[dict[str, Any]]:
    settings = payload.get("settings", {})
    pr = payload.get("pull_request", {})
    records: list[dict[str, Any]] = []
    for item in settings.get("repository_memory", []):
        if isinstance(item, dict):
            records.append(record_from_repository_memory(item, payload.get("analysis_run_id", "")))
    records.append(
        make_record(
            "current_pr",
            "current-pr",
            " ".join(str(part) for part in [pr.get("title"), pr.get("body")] if part),
            {"type": "current_pr", "path": "", "analysis_run_id": payload.get("analysis_run_id", "")},
        )
    )
    for file in files:
        path = normalize_path(file.get("path", ""))
        text = " ".join(
            str(part)
            for part in [
                path,
                file.get("classification"),
                " ".join(file.get("risk_reasons", [])),
                file.get("patch") or file.get("content") or "",
            ]
            if part
        )
        if text.strip():
            records.append(
                make_record(
                    "changed_file",
                    path,
                    text,
                    {
                        "type": "changed_file",
                        "path": path,
                        "classification": file.get("classification", ""),
                        "analysis_run_id": payload.get("analysis_run_id", ""),
                    },
                )
            )
    for item in intent_items:
        records.append(
            make_record(
                "intent",
                item.get("id", "intent"),
                item.get("text", ""),
                {
                    "type": "intent",
                    "intent_id": item.get("id", ""),
                    "category": item.get("category", ""),
                    "analysis_run_id": payload.get("analysis_run_id", ""),
                },
            )
        )
    for delta in semantic.get("behavioral_deltas", [])[:12]:
        records.append(
            make_record(
                "behavior",
                f"{delta.get('path', 'behavior')}:{delta.get('symbol', 'symbol')}",
                " ".join(str(delta.get(key, "")) for key in ["path", "symbol", "new_behavior", "divergent_input"]),
                {
                    "type": "behavior",
                    "path": delta.get("path", ""),
                    "severity": delta.get("severity", ""),
                    "analysis_run_id": payload.get("analysis_run_id", ""),
                },
            )
        )
    for finding in concept.get("concept_findings", [])[:12]:
        records.append(
            make_record(
                "concept",
                f"{finding.get('concept', 'concept')}:{finding.get('path', '')}",
                " ".join(str(finding.get(key, "")) for key in ["concept", "message", "path"]),
                {
                    "type": "concept",
                    "path": finding.get("path", ""),
                    "concept": finding.get("concept", ""),
                    "analysis_run_id": payload.get("analysis_run_id", ""),
                },
            )
        )
    return [record for record in records if record.get("text")]


def record_from_repository_memory(item: dict[str, Any], analysis_run_id: str) -> dict[str, Any]:
    kind = str(item.get("type") or item.get("kind") or infer_kind(item.get("path", "")))
    path = normalize_path(item.get("path", ""))
    title = str(item.get("title") or path or kind)
    text = " ".join(str(part) for part in [title, item.get("summary"), item.get("text")] if part)
    metadata = {
        "type": kind,
        "path": path,
        "title": title,
        "source": str(item.get("source", "repository_memory")),
        "analysis_run_id": str(item.get("analysis_run_id", "")),
        "repository_memory": True,
    }
    if item.get("url"):
        metadata["url"] = str(item["url"])
    if item.get("tests"):
        metadata["tests"] = item["tests"]
    if item.get("pr_number"):
        metadata["pr_number"] = str(item["pr_number"])
    if not metadata["analysis_run_id"]:
        metadata["seeded_for_run"] = analysis_run_id
    return make_record(kind, title, text, metadata)


def make_record(kind: str, key: str, text: str, metadata: dict[str, Any]) -> dict[str, Any]:
    label_key = short_hash(f"{kind}:{key}:{text[:180]}")
    return {
        "label": f"mergeguard:{kind}:{label_key}",
        "text": text.strip(),
        "metadata": {
            **metadata,
            "type": metadata.get("type") or kind,
            "risk_terms": important_terms(text)[:10],
        },
    }


def memory_queries(
    pr: dict[str, Any],
    files: list[dict[str, Any]],
    intent_items: list[dict[str, Any]],
    semantic: dict[str, Any],
) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for item in intent_items[:6]:
        queries.append(
            {
                "id": item.get("id", f"intent-{len(queries) + 1}"),
                "type": "intent",
                "query": item.get("text", ""),
                "top_k": 7,
            }
        )
    hotspot_terms = " ".join(
        " ".join([file.get("path", ""), " ".join(file.get("risk_reasons", []))])
        for file in files[:8]
        if file.get("risk_score", 0) >= 40 or file.get("classification") in {"prompt", "security-sensitive"}
    )
    if hotspot_terms:
        queries.append({"id": "risk-hotspots", "type": "risk", "query": hotspot_terms, "top_k": 8})
    behavior_terms = " ".join(
        " ".join(str(delta.get(key, "")) for key in ["path", "new_behavior", "divergent_input"])
        for delta in semantic.get("behavioral_deltas", [])[:6]
    )
    if behavior_terms:
        queries.append({"id": "behavior-deltas", "type": "behavior", "query": behavior_terms, "top_k": 6})
    title_query = " ".join(str(part) for part in [pr.get("title", ""), pr.get("body", "")] if part)
    if title_query:
        queries.append({"id": "pr-summary", "type": "pr", "query": title_query, "top_k": 6})
    return [query for query in queries if query.get("query")][:10]


def normalize_memory_hit(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    text = str(item.get("text") or item.get("content") or item.get("summary") or "")
    path = normalize_path(str(metadata.get("path") or item.get("path") or ""))
    kind = str(metadata.get("type") or item.get("type") or infer_kind(path))
    score = item.get("score", item.get("similarity", item.get("relevance", 0.72)))
    try:
        score_value = round(float(score), 3)
    except (TypeError, ValueError):
        score_value = 0.72
    return {
        "label": str(item.get("label") or metadata.get("label") or short_hash(text)),
        "kind": kind,
        "title": str(metadata.get("title") or item.get("title") or path or kind),
        "path": path,
        "text": text[:900],
        "score": min(max(score_value, 0.0), 1.0),
        "source": str(metadata.get("source") or item.get("source") or "memory"),
        "url": metadata.get("url") or item.get("url"),
        "pr_number": metadata.get("pr_number"),
        "risk_terms": metadata.get("risk_terms") or important_terms(text)[:8],
        "metadata": metadata,
    }


def infer_kind(path: str) -> str:
    clean = normalize_path(path).lower()
    if is_test(clean):
        return "test"
    if is_docs(clean):
        return "doc"
    if "policy" in clean:
        return "policy"
    if "contract" in clean:
        return "contract"
    return "memory"


def is_current_run_hit(hit: dict[str, Any], analysis_run_id: str) -> bool:
    if not analysis_run_id:
        return False
    metadata = hit.get("metadata") or {}
    return metadata.get("analysis_run_id") == analysis_run_id and not metadata.get("repository_memory")


def is_repository_evidence_hit(hit: dict[str, Any], analysis_run_id: str) -> bool:
    if is_current_run_hit(hit, analysis_run_id):
        return False
    metadata = hit.get("metadata") or {}
    if metadata.get("repository_memory"):
        return True
    if hit.get("kind") in {"test", "doc", "prior_pr", "policy", "contract", "runbook"}:
        return True
    return not metadata.get("analysis_run_id")


def dedupe_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for hit in hits:
        key = hit.get("label") or f"{hit.get('kind')}:{hit.get('path')}:{hit.get('title')}"
        existing = deduped.get(key)
        if existing is None or hit.get("score", 0) > existing.get("score", 0):
            deduped[key] = hit
    return sorted(deduped.values(), key=lambda item: item.get("score", 0), reverse=True)


def requirement_memory(item: dict[str, Any], hits: list[dict[str, Any]]) -> dict[str, Any]:
    terms = set(item.get("terms") or important_terms(item.get("text", "")))
    matched = []
    for hit in hits:
        haystack = " ".join(
            str(part)
            for part in [hit.get("title", ""), hit.get("path", ""), hit.get("text", ""), " ".join(hit.get("risk_terms", []))]
        ).lower()
        overlap = sorted(term for term in terms if term in haystack)
        if overlap or hit.get("score", 0) >= 0.7:
            matched.append({**hit, "matched_terms": overlap[:8]})
    matched = sorted(matched, key=lambda hit: (len(hit.get("matched_terms", [])), hit.get("score", 0)), reverse=True)[:6]
    test_candidates = [hit for hit in matched if hit.get("kind") == "test"]
    doc_candidates = [hit for hit in matched if hit.get("kind") in {"doc", "runbook", "policy"}]
    if test_candidates:
        status = "found"
    elif matched:
        status = "partial"
    else:
        status = "missing"
    confidence = 0.86 if status == "found" else 0.68 if status == "partial" else 0.45
    return {
        "intent_id": item.get("id"),
        "intent_text": item.get("text", ""),
        "status": status,
        "confidence": confidence,
        "matches": matched,
        "test_candidates": test_candidates,
        "doc_candidates": doc_candidates,
        "suggested_action": requirement_suggestion(item, status, test_candidates, doc_candidates),
    }


def requirement_suggestion(
    item: dict[str, Any],
    status: str,
    test_candidates: list[dict[str, Any]],
    doc_candidates: list[dict[str, Any]],
) -> str:
    if test_candidates:
        return f"Review or extend `{test_candidates[0]['path']}` for this PR intent."
    if doc_candidates:
        return f"Use `{doc_candidates[0]['path'] or doc_candidates[0]['title']}` as acceptance evidence, then link a test."
    return f"Index or add evidence for PR intent: {item.get('text', '')}"


def memory_findings(requirement_evidence: list[dict[str, Any]], files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    has_high_risk_source = any(
        file.get("risk_score", 0) >= 45
        and file.get("classification") not in {"test", "docs", "generated"}
        for file in files
    )
    for item in requirement_evidence:
        if item["status"] == "missing":
            findings.append(
                {
                    "severity": "review_required" if has_high_risk_source else "warn",
                    "message": f"No repository memory found for intent: {item['intent_text']}",
                    "intent_id": item.get("intent_id"),
                    "suggested_action": item["suggested_action"],
                }
            )
    return findings


def recommended_test_updates(
    related_tests: list[dict[str, Any]],
    requirement_evidence: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    targets = [
        file
        for file in files
        if file.get("classification") not in {"test", "docs", "generated"} and file.get("status") != "removed"
    ]
    recommendations = []
    seen: set[str] = set()
    for evidence in requirement_evidence:
        for test in evidence.get("test_candidates", [])[:2]:
            path = test.get("path") or test.get("title")
            if not path or path in seen:
                continue
            seen.add(path)
            recommendations.append(
                {
                    "path": path,
                    "framework": "repo-memory",
                    "intent": f"Extend repository test evidence for: {evidence.get('intent_text', '')}",
                    "memory_score": test.get("score"),
                }
            )
    if not recommendations:
        for test in related_tests[:3]:
            path = test.get("path") or test.get("title")
            if path:
                recommendations.append(
                    {
                        "path": path,
                        "framework": "repo-memory",
                        "intent": f"Check this existing test against {targets[0]['path'] if targets else 'the PR intent'}.",
                        "memory_score": test.get("score"),
                    }
                )
    return recommendations[:8]


def source_type_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        source_type = str(record.get("metadata", {}).get("type", "memory"))
        counts[source_type] = counts.get(source_type, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def memory_provider() -> str:
    if isinstance(app, LocalAgentApp):
        return "local-demo"
    return "magenta-platform"


def platform_capabilities() -> list[str]:
    if isinstance(app, LocalAgentApp):
        return ["local-lexical-memory", "deterministic-demo-fallback"]
    return ["magenta-memory", "voyage-embeddings", "atlas-vector-search"]


def memory_confidence(output: dict[str, Any]) -> float:
    matches = len(output.get("semantic_matches", []))
    related_tests = len(output.get("related_tests", []))
    findings = len(output.get("memory_findings", []))
    score = 0.58 + min(0.22, matches * 0.015) + min(0.14, related_tests * 0.035) - min(0.18, findings * 0.045)
    return round(max(0.35, min(score, 0.92)), 2)


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


register_entrypoint(app, run)


def main() -> None:
    """Run the Magenta agent service when executed by agentic dev/deploy."""
    if not hasattr(app, "run"):
        raise RuntimeError(
            "Magenta SDK is required to run this agent service. "
            "Use the local orchestrator for demo mode."
        )
    app.run()


if __name__ == "__main__":
    main()
