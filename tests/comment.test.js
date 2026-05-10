import test from "node:test";
import assert from "node:assert/strict";
import { renderReviewComment, STICKY_COMMENT_MARKER } from "../packages/shared/src/comment.js";

test("review comment includes sticky marker and concise sections", () => {
  const body = renderReviewComment({
    pr: { number: 1, title: "Example" },
    run: { id: "run_1" },
    dashboardUrl: "http://localhost:4000/?pr=pr_1",
    summary: {
      status: "review",
      risk_score: 72,
      top_blocker: "Risky source change has no changed test evidence in this PR.",
      next_action: "Add tests.",
      file_groups: {
        must_inspect: [{ path: "auth/session.ts", classification: "security-sensitive", risk_score: 60, risk_reasons: ["auth change"] }],
        safe_to_skim: [{ path: "README.md", classification: "docs", risk_score: 4, risk_reasons: ["docs"] }]
      },
      evidence_findings: [
        {
          path: "auth/session.ts",
          message: "Risky source change has no changed test evidence in this PR.",
          suggested_action: "Add auth tests."
        }
      ],
      checklist: ["Inspect auth/session.ts first."]
    }
  });

  assert.match(body, new RegExp(STICKY_COMMENT_MARKER));
  assert.match(body, /MergeGuard Review Brief/);
  assert.match(body, /Risk Hotspots/);
  assert.match(body, /Safe To Skim/);
});
