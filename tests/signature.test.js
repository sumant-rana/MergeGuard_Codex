import test from "node:test";
import assert from "node:assert/strict";
import { signGitHubBody, verifyGitHubSignature } from "../packages/shared/src/signature.js";

test("verifies GitHub SHA-256 webhook signatures", () => {
  const body = Buffer.from(JSON.stringify({ hello: "world" }));
  const signature = signGitHubBody(body, "secret");

  assert.equal(verifyGitHubSignature(body, signature, "secret").ok, true);
  assert.equal(verifyGitHubSignature(body, signature, "wrong").ok, false);
});
