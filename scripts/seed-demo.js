import { readFile } from "node:fs/promises";
import { signGitHubBody } from "../packages/shared/src/signature.js";

const target = process.env.MERGEGUARD_PUBLIC_URL || "http://localhost:4000";
const secret = process.env.GITHUB_WEBHOOK_SECRET || "change-me";
const base = JSON.parse(await readFile("fixtures/webhooks/pull_request_opened.json", "utf8"));
const docsOnlyFiles = JSON.parse(await readFile("fixtures/changed-files/docs-only.json", "utf8"));

const payloads = [
  base,
  {
    ...structuredClone(base),
    pull_request: {
      ...base.pull_request,
      number: 43,
      title: "Refresh review documentation",
      body: "Updates review process documentation.",
      html_url: "https://github.com/acme/checkout/pull/43",
      head: { sha: "head-sha-docs-001" },
      base: { sha: "base-sha-docs-001" },
      user: { login: "samira" }
    },
    mergeguard: {
      changed_files: docsOnlyFiles
    }
  }
];

for (const payload of payloads) {
  const body = JSON.stringify(payload);
  const response = await fetch(`${target}/webhooks/github`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-github-event": "pull_request",
      "x-github-delivery": `seed-${payload.pull_request.number}-${Date.now()}`,
      "x-hub-signature-256": signGitHubBody(body, secret)
    },
    body
  });
  process.stdout.write(`#${payload.pull_request.number}: ${response.status} ${response.statusText}\n`);
}
