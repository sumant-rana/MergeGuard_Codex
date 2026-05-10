import { createServer as createHttpServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize, resolve } from "node:path";
import { verifyGitHubSignature } from "../../../packages/shared/src/signature.js";
import { processGitHubWebhook, rerunAnalysisForPullRequest } from "./pipeline.js";

const STATIC_DIR = resolve(process.cwd(), "apps/web/public");

export function createServer({ store, github, config }) {
  return createHttpServer(async (req, res) => {
    try {
      const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);

      if (req.method === "GET" && url.pathname === "/health") {
        return sendJson(res, 200, { ok: true, service: "mergeguard-api" });
      }

      if (req.method === "POST" && url.pathname === "/webhooks/github") {
        return handleGitHubWebhook({ req, res, store, github, config });
      }

      if (url.pathname.startsWith("/api/")) {
        return handleApi({ req, res, url, store, github, config });
      }

      if (req.method === "GET") {
        return serveStatic(res, url.pathname);
      }

      return sendJson(res, 404, { error: "not found" });
    } catch (error) {
      console.error(error);
      return sendJson(res, 500, { error: error.message });
    }
  });
}

async function handleGitHubWebhook({ req, res, store, github, config }) {
  const rawBody = await readBody(req);
  const signature = req.headers["x-hub-signature-256"];
  const verification = verifyGitHubSignature(rawBody, signature, config.github.webhookSecret);
  if (!verification.ok) {
    return sendJson(res, 401, { error: verification.reason });
  }

  const eventName = req.headers["x-github-event"] || "unknown";
  const deliveryId = req.headers["x-github-delivery"] || null;
  const payload = JSON.parse(rawBody.toString("utf8") || "{}");
  const result = await processGitHubWebhook({ eventName, deliveryId, payload, store, github, config });
  return sendJson(res, result.status === "failed" ? 500 : 202, result);
}

async function handleApi({ req, res, url, store, github, config }) {
  if (req.method === "GET" && url.pathname === "/api/installations") {
    return sendJson(res, 200, { installations: store.listInstallations() });
  }

  if (req.method === "GET" && url.pathname === "/api/repos") {
    return sendJson(res, 200, { repositories: store.listRepositories() });
  }

  if (req.method === "GET" && url.pathname === "/api/prs") {
    return sendJson(res, 200, {
      pull_requests: store.listPullRequests({
        repo: url.searchParams.get("repo"),
        owner: url.searchParams.get("owner"),
        risk_state: url.searchParams.get("risk_state"),
        label: url.searchParams.get("label")
      })
    });
  }

  const prMatch = url.pathname.match(/^\/api\/prs\/([^/]+)$/);
  if (req.method === "GET" && prMatch) {
    const pr = store.getPullRequest(prMatch[1]);
    return pr ? sendJson(res, 200, { pull_request: pr }) : sendJson(res, 404, { error: "pull request not found" });
  }

  const rerunMatch = url.pathname.match(/^\/api\/prs\/([^/]+)\/rerun$/);
  if (req.method === "POST" && rerunMatch) {
    const run = await rerunAnalysisForPullRequest({ prId: rerunMatch[1], store, github, config });
    return run ? sendJson(res, 202, { run }) : sendJson(res, 404, { error: "pull request not found" });
  }

  if (req.method === "GET" && url.pathname === "/api/policy-packs") {
    return sendJson(res, 200, { policy_packs: store.listPolicyPacks(url.searchParams.get("repo_id")) });
  }

  if (req.method === "POST" && url.pathname === "/api/policy-packs") {
    const body = JSON.parse((await readBody(req)).toString("utf8") || "{}");
    const pack = await store.createPolicyPack(body);
    return sendJson(res, 201, { policy_pack: pack });
  }

  const activatePolicyMatch = url.pathname.match(/^\/api\/policy-packs\/([^/]+)\/activate$/);
  if (req.method === "POST" && activatePolicyMatch) {
    const pack = await store.activatePolicyPack(activatePolicyMatch[1]);
    return pack ? sendJson(res, 202, { policy_pack: pack }) : sendJson(res, 404, { error: "policy pack not found" });
  }

  const overrideMatch = url.pathname.match(/^\/api\/findings\/([^/]+)\/override$/);
  if (req.method === "POST" && overrideMatch) {
    const body = JSON.parse((await readBody(req)).toString("utf8") || "{}");
    if (!body.reason) return sendJson(res, 400, { error: "override reason is required" });
    if (!body.run_id) return sendJson(res, 400, { error: "run_id is required" });
    const override = await store.recordReviewerOverride({
      finding_id: overrideMatch[1],
      run_id: body.run_id,
      reviewer: body.reviewer,
      reason: body.reason
    });
    return sendJson(res, 201, { override });
  }

  if (req.method === "POST" && url.pathname === "/api/outcomes") {
    const body = JSON.parse((await readBody(req)).toString("utf8") || "{}");
    if (!body.pr_id || !body.outcome_type) return sendJson(res, 400, { error: "pr_id and outcome_type are required" });
    const outcome = await store.recordPostMergeOutcome(body);
    return sendJson(res, 201, { outcome });
  }

  if (req.method === "GET" && url.pathname === "/api/metrics") {
    return sendJson(res, 200, store.metrics());
  }

  const runMatch = url.pathname.match(/^\/api\/runs\/([^/]+)$/);
  if (req.method === "GET" && runMatch) {
    const run = store.getRun(runMatch[1]);
    return run ? sendJson(res, 200, { run }) : sendJson(res, 404, { error: "analysis run not found" });
  }

  const auditMatch = url.pathname.match(/^\/api\/audit\/pr\/([^/]+)$/);
  if (req.method === "GET" && auditMatch) {
    const audit = store.auditForPullRequest(auditMatch[1]);
    return audit ? sendJson(res, 200, audit) : sendJson(res, 404, { error: "pull request not found" });
  }

  return sendJson(res, 404, { error: "api route not found" });
}

async function serveStatic(res, pathname) {
  const requested = pathname === "/" ? "/index.html" : pathname;
  const clean = normalize(requested).replace(/^(\.\.[/\\])+/, "");
  const filePath = resolve(join(STATIC_DIR, clean));
  if (!filePath.startsWith(STATIC_DIR)) return sendJson(res, 403, { error: "forbidden" });

  try {
    const body = await readFile(filePath);
    res.writeHead(200, { "Content-Type": contentType(filePath), "Cache-Control": "no-store" });
    res.end(body);
  } catch (error) {
    if (error.code === "ENOENT") {
      const index = await readFile(join(STATIC_DIR, "index.html"));
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
      res.end(index);
      return;
    }
    throw error;
  }
}

function sendJson(res, status, data) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(`${JSON.stringify(data, null, 2)}\n`);
}

function readBody(req) {
  return new Promise((resolveBody, reject) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("error", reject);
    req.on("end", () => resolveBody(Buffer.concat(chunks)));
  });
}

function contentType(filePath) {
  const types = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml"
  };
  return types[extname(filePath)] || "application/octet-stream";
}
