import { GitHubClient } from "./github.js";
import { loadDotEnv, readConfig } from "./env.js";
import { JsonStore } from "./store.js";
import { createServer } from "./server.js";

loadDotEnv();

const config = readConfig();
const store = new JsonStore(config.dataFile);
await store.init();

const github = new GitHubClient(config.github);
const server = createServer({ store, github, config });

server.listen(config.port, () => {
  console.log(`MergeGuard Command Center listening on ${config.publicUrl}`);
  console.log(`Dashboard: ${config.publicUrl}`);
  console.log(`Webhook:   ${config.publicUrl}/webhooks/github`);
  console.log(`Mode:      ${config.checkMode}`);
});
