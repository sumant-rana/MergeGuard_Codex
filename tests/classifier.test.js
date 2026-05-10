import test from "node:test";
import assert from "node:assert/strict";
import { classifyChangedFile } from "../packages/shared/src/classifier.js";
import { analyzePullRequest } from "../workers/analyzer/src/analyze.js";

test("classifies risky payment source above docs-only changes", () => {
  const risky = classifyChangedFile({
    path: "payments/refund_processor.ts",
    additions: 80,
    deletions: 20,
    changes: 100
  });
  const docs = classifyChangedFile({
    path: "docs/refund-flow.md",
    additions: 80,
    deletions: 20,
    changes: 100
  });

  assert.equal(risky.classification, "security-sensitive");
  assert.equal(docs.classification, "docs");
  assert.ok(risky.risk_score > docs.risk_score);
  assert.equal(risky.must_inspect, true);
});

test("analysis emits missing-evidence finding for risky source without tests", () => {
  const result = analyzePullRequest({
    changedFiles: [
      { path: "auth/session_tokens.ts", additions: 20, deletions: 5, changes: 25 },
      { path: "README.md", additions: 3, deletions: 0, changes: 3 }
    ]
  });

  assert.equal(result.summary.status, "review");
  assert.equal(result.summary.evidence_findings.length, 1);
  assert.match(result.summary.top_blocker, /no changed test evidence/i);
});

test("docs-only analysis passes with lower risk", () => {
  const result = analyzePullRequest({
    changedFiles: [
      { path: "docs/review.md", additions: 20, deletions: 2, changes: 22 },
      { path: "README.md", additions: 4, deletions: 1, changes: 5 }
    ]
  });

  assert.equal(result.summary.status, "pass");
  assert.ok(result.summary.risk_score < 20);
});
