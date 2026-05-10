import { createSign } from "node:crypto";
import { readFile } from "node:fs/promises";

const API_VERSION = "2022-11-28";

export class GitHubClient {
  constructor(config) {
    this.config = config || {};
    this.installationTokens = new Map();
  }

  get enabled() {
    return Boolean(this.config.token || (this.config.appId && (this.config.privateKey || this.config.privateKeyPath)));
  }

  async listPullFiles({ owner, repo, pullNumber, installationId }) {
    const token = await this.getToken(installationId);
    return this.request({
      method: "GET",
      path: `/repos/${owner}/${repo}/pulls/${pullNumber}/files?per_page=100`,
      token
    });
  }

  async createCheckRun({ owner, repo, headSha, name, conclusion, summary, text, detailsUrl, installationId }) {
    const token = await this.getToken(installationId);
    return this.request({
      method: "POST",
      path: `/repos/${owner}/${repo}/check-runs`,
      token,
      body: {
        name,
        head_sha: headSha,
        status: "completed",
        conclusion,
        details_url: detailsUrl,
        output: {
          title: name,
          summary,
          text
        }
      }
    });
  }

  async upsertStickyComment({ owner, repo, pullNumber, marker, body, installationId }) {
    const token = await this.getToken(installationId);
    const comments = await this.request({
      method: "GET",
      path: `/repos/${owner}/${repo}/issues/${pullNumber}/comments?per_page=100`,
      token
    });
    const existing = comments.find((comment) => String(comment.body || "").includes(marker));
    if (existing) {
      return this.request({
        method: "PATCH",
        path: `/repos/${owner}/${repo}/issues/comments/${existing.id}`,
        token,
        body: { body }
      });
    }
    return this.request({
      method: "POST",
      path: `/repos/${owner}/${repo}/issues/${pullNumber}/comments`,
      token,
      body: { body }
    });
  }

  async syncLabels({ owner, repo, pullNumber, desiredLabels, managedLabels, installationId }) {
    const token = await this.getToken(installationId);
    for (const label of Object.values(managedLabels)) {
      try {
        await this.request({
          method: "POST",
          path: `/repos/${owner}/${repo}/labels`,
          token,
          body: label
        });
      } catch (error) {
        if (error.status !== 422) throw error;
      }
    }

    if (desiredLabels.length) {
      await this.request({
        method: "POST",
        path: `/repos/${owner}/${repo}/issues/${pullNumber}/labels`,
        token,
        body: { labels: desiredLabels }
      });
    }

    const desired = new Set(desiredLabels);
    for (const label of Object.values(managedLabels)) {
      if (desired.has(label.name)) continue;
      try {
        await this.request({
          method: "DELETE",
          path: `/repos/${owner}/${repo}/issues/${pullNumber}/labels/${encodeURIComponent(label.name)}`,
          token
        });
      } catch (error) {
        if (![404, 422].includes(error.status)) throw error;
      }
    }
  }

  async getToken(installationId) {
    if (this.config.token) return this.config.token;
    if (!installationId) throw new Error("GitHub installation id is required for GitHub App authentication.");

    const cached = this.installationTokens.get(String(installationId));
    if (cached && cached.expiresAt > Date.now() + 60_000) return cached.token;

    const jwt = await this.createAppJwt();
    const response = await this.request({
      method: "POST",
      path: `/app/installations/${installationId}/access_tokens`,
      token: jwt,
      authScheme: "Bearer"
    });
    const expiresAt = Date.parse(response.expires_at);
    this.installationTokens.set(String(installationId), { token: response.token, expiresAt });
    return response.token;
  }

  async createAppJwt() {
    const now = Math.floor(Date.now() / 1000);
    const header = base64Url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
    const payload = base64Url(
      JSON.stringify({
        iat: now - 60,
        exp: now + 540,
        iss: this.config.appId
      })
    );
    const privateKey = await this.readPrivateKey();
    const signer = createSign("RSA-SHA256");
    signer.update(`${header}.${payload}`);
    signer.end();
    return `${header}.${payload}.${signer.sign(privateKey, "base64url")}`;
  }

  async readPrivateKey() {
    if (this.config.privateKey) return this.config.privateKey.replace(/\\n/g, "\n");
    if (!this.config.privateKeyPath) throw new Error("GITHUB_APP_PRIVATE_KEY_PATH is not configured.");
    return readFile(this.config.privateKeyPath, "utf8");
  }

  async request({ method, path, token, body, authScheme = "token", attempt = 0 }) {
    const response = await fetch(`https://api.github.com${path}`, {
      method,
      headers: {
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "mergeguard-command-center",
        ...(token ? { Authorization: `${authScheme} ${token}` } : {}),
        ...(body ? { "Content-Type": "application/json" } : {})
      },
      body: body ? JSON.stringify(body) : undefined
    });

    if (response.status === 204) return null;

    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (!response.ok) {
      const remaining = response.headers.get("x-ratelimit-remaining");
      const reset = response.headers.get("x-ratelimit-reset");
      if ((response.status === 403 || response.status === 429) && remaining === "0" && attempt < 2) {
        const waitMs = Math.min(2_000, Math.max(250, Number(reset || 0) * 1000 - Date.now()));
        await sleep(waitMs);
        return this.request({ method, path, token, body, authScheme, attempt: attempt + 1 });
      }
      const error = new Error(data?.message || `GitHub API request failed: ${method} ${path}`);
      error.status = response.status;
      error.rateLimitRemaining = response.headers.get("x-ratelimit-remaining");
      error.rateLimitReset = response.headers.get("x-ratelimit-reset");
      error.response = data;
      throw error;
    }
    return data;
  }
}

function base64Url(value) {
  return Buffer.from(value).toString("base64url");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
