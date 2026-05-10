import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { JsonStore } from "../apps/api/src/store.js";
import { processGitHubWebhook } from "../apps/api/src/pipeline.js";
import { GitHubClient } from "../apps/api/src/github.js";

test("pull request webhook creates one analysis run and ignores duplicate delivery", async () => {
  const dir = await mkdtemp(join(tmpdir(), "mergeguard-test-"));
  try {
    const store = new JsonStore(join(dir, "data.json"));
    await store.init();
    const payload = JSON.parse(await readFile("fixtures/webhooks/pull_request_opened.json", "utf8"));
    const config = {
      publicUrl: "http://localhost:4000",
      checkMode: "advisory",
      allowFixtureFiles: true,
      github: {}
    };
    const github = new GitHubClient(config.github);

    const first = await processGitHubWebhook({
      eventName: "pull_request",
      deliveryId: "delivery-1",
      payload,
      store,
      github,
      config
    });
    const second = await processGitHubWebhook({
      eventName: "pull_request",
      deliveryId: "delivery-1",
      payload,
      store,
      github,
      config
    });

    assert.equal(first.status, "processed");
    assert.equal(second.status, "duplicate");
    assert.equal(store.state.analysisRuns.length, 1);
    assert.equal(store.state.changedFiles.length, 5);
    assert.equal(store.listPullRequests()[0].latest_run.summary.status, "review");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
