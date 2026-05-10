import { createHash, randomUUID } from "node:crypto";

export function newId(prefix) {
  return `${prefix}_${randomUUID()}`;
}

export function stableHash(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

export function buildWebhookIdempotencyKey({ deliveryId, repositoryId, pullNumber, headSha, action }) {
  return [
    deliveryId || "no-delivery",
    repositoryId || "no-repo",
    pullNumber || "no-pr",
    headSha || "no-head",
    action || "no-action"
  ].join(":");
}
