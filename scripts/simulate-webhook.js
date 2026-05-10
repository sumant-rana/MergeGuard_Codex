import { readFile } from "node:fs/promises";
import { signGitHubBody } from "../packages/shared/src/signature.js";

const target = process.env.MERGEGUARD_PUBLIC_URL || "http://localhost:4000";
const secret = process.env.GITHUB_WEBHOOK_SECRET || "change-me";
const fixturePath = process.argv[2] || "fixtures/webhooks/pull_request_opened.json";
const delivery = process.env.GITHUB_DELIVERY_ID || `local-${Date.now()}`;
const body = await readFile(fixturePath, "utf8");
const response = await fetch(`${target}/webhooks/github`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    "x-github-event": "pull_request",
    "x-github-delivery": delivery,
    "x-hub-signature-256": signGitHubBody(body, secret)
  },
  body
});

const text = await response.text();
process.stdout.write(`${response.status} ${response.statusText}\n${text}`);
