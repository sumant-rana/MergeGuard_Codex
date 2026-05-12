# Local GitHub PR Workflow

You can process a real GitHub PR locally without webhooks. The helper script uses the GitHub CLI to create or inspect a PR, collects PR metadata, changed files, patches, linked issues, commit history, and CODEOWNERS, then posts that payload to the local MergeGuard API.

## Prerequisites

- `gh` installed and authenticated.
- Target repo checked out locally.
- MergeGuard dashboard API running:

```sh
cd /Users/sumant.rana/Sumant/workspace/codex/skunk/mergeGuard
python3 apps/api/main.py
```

Open:

```text
http://127.0.0.1:4100
```

If `gh` is installed outside your shell `PATH`, set:

```sh
export GH_BIN=/absolute/path/to/gh
```

On Apple Silicon Homebrew this is usually:

```sh
export GH_BIN=/opt/homebrew/bin/gh
```

## Analyze An Existing PR

From the MergeGuard repo:

```sh
scripts/mergeguard_pr.py analyze \
  --repo /absolute/path/to/target-repo \
  --pr 123
```

You can pass a PR URL, number, branch, or omit `--pr` to analyze the PR for the current branch:

```sh
scripts/mergeguard_pr.py analyze --repo /absolute/path/to/target-repo
```

## Create A PR And Analyze It

From the MergeGuard repo:

```sh
scripts/mergeguard_pr.py create \
  --repo /absolute/path/to/target-repo \
  --base main \
  --title "Fix refund retry handling" \
  --body "Fixes #123. Ensure retry failure stays idempotent."
```

The script runs `gh pr create`, then immediately calls:

```text
POST http://127.0.0.1:4100/api/github/pr/analyze
```

## Dry Run The Payload

To inspect what will be sent without invoking MergeGuard:

```sh
scripts/mergeguard_pr.py analyze \
  --repo /absolute/path/to/target-repo \
  --pr 123 \
  --payload-out /tmp/mergeguard-pr.json \
  --no-post
```

## API Contract

The endpoint accepts this shape:

```json
{
  "repository": {
    "owner": "acme",
    "name": "checkout",
    "full_name": "acme/checkout",
    "default_branch": "main"
  },
  "pull_request": {
    "number": 123,
    "title": "Fix refund retry handling",
    "body": "Fixes #456. Ensure retry failure stays idempotent.",
    "author": "alice",
    "base_ref": "main",
    "head_ref": "refund-retry",
    "base_sha": "base",
    "head_sha": "head",
    "issue_refs": [{"number": 456, "title": "Refund retry fails"}],
    "commit_history": [{"oid": "abc123", "message": "Fix refund retry"}]
  },
  "changed_files": [
    {
      "path": "payments/refund_retry.ts",
      "status": "modified",
      "additions": 10,
      "deletions": 2,
      "patch": "@@ ...",
      "content": "current file content"
    }
  ],
  "settings": {
    "codeowners": "payments/ @payments-team"
  }
}
```

The API normalizes the payload, runs all nine agents, stores the run, and updates the dashboard queue.

## Webhook Alternative

This local CLI workflow does not require a webhook or a public tunnel. If you want GitHub to notify MergeGuard automatically for PRs created from any machine, add a GitHub webhook and expose local MergeGuard with `smee`, `ngrok`, or `cloudflared`.
