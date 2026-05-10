import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

export function loadDotEnv(file = ".env") {
  const path = resolve(process.cwd(), file);
  if (!existsSync(path)) return;

  const lines = readFileSync(path, "utf8").split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const index = trimmed.indexOf("=");
    if (index === -1) continue;
    const key = trimmed.slice(0, index).trim();
    const raw = trimmed.slice(index + 1).trim();
    if (!key || process.env[key] !== undefined) continue;
    process.env[key] = raw.replace(/^['"]|['"]$/g, "");
  }
}

export function readConfig() {
  const publicUrl = process.env.MERGEGUARD_PUBLIC_URL || `http://localhost:${process.env.PORT || 4000}`;

  return {
    port: Number(process.env.PORT || 4000),
    publicUrl,
    dataFile: process.env.MERGEGUARD_DATA_FILE || "./data/mergeguard.json",
    checkMode: process.env.MERGEGUARD_CHECK_MODE === "blocking" ? "blocking" : "advisory",
    allowFixtureFiles: process.env.MERGEGUARD_ALLOW_FIXTURE_FILES !== "false",
    github: {
      webhookSecret: process.env.GITHUB_WEBHOOK_SECRET || "",
      appId: process.env.GITHUB_APP_ID || "",
      privateKeyPath: process.env.GITHUB_APP_PRIVATE_KEY_PATH || "",
      privateKey: process.env.GITHUB_APP_PRIVATE_KEY || "",
      token: process.env.GITHUB_TOKEN || ""
    }
  };
}
