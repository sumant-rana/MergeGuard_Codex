#!/usr/bin/env bash
# Patch the agentic-cli-generated compose so the memory-server containers
# actually start.
#
# Why: `agentic dev up --all` (0.1.28-alpha) emits memory-server entries
# that use `image: runner-base:...` directly. The base image's CMD is
# /bin/bash, which exits immediately. Each app-* service uses
# `build: { dockerfile: .agentic/Dockerfile.dev }` which sets CMD to
# /agentic/dev-entrypoint.py. We convert each memory-server entry to
# use the same Dockerfile.dev so it inherits the working CMD.
#
# Idempotent: safe to re-run after `agentic dev up --all` regenerates
# the compose file.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/.agentic/docker-compose.dev.all.yml"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
    echo "✗ ${COMPOSE_FILE} not found — run 'agentic dev up --all' once to generate it." >&2
    exit 1
fi

# Pattern: each memory-server service has exactly one line matching
# `    image: "${RUNNER_BASE_IMAGE:-...}"`. Replace each such line with a
# `build:` block pointing at the same Dockerfile.dev that app-* uses.

count=$(grep -c '^    image: "\${RUNNER_BASE_IMAGE:-' "${COMPOSE_FILE}" || true)

if [[ "${count}" -eq 0 ]]; then
    echo "✓ already patched — no RUNNER_BASE_IMAGE entries remain."
    exit 0
fi

echo "→ patching ${count} memory-server entr$([[ ${count} -eq 1 ]] && echo 'y' || echo 'ies') in ${COMPOSE_FILE}"

# Use a Python one-liner (no GNU sed/awk dependency on macOS).
python3 - "${COMPOSE_FILE}" <<'PY'
import sys, pathlib

path = pathlib.Path(sys.argv[1])
text = path.read_text()

OLD = '    image: "${RUNNER_BASE_IMAGE:-ghcr.io/10gen/magenta-client-libraries/runner-base:0.1.28-alpha}"'
NEW = """    # Patched by scripts/patch-compose.sh: use the same Dockerfile.dev as
    # app-* services so the /agentic/dev-entrypoint.py CMD is set. The
    # agentic-cli-generated `image:` line alone leaves the base image's
    # default CMD (/bin/bash), which exits immediately and breaks the
    # memory-server dependency.
    build:
      context: ..
      dockerfile: .agentic/Dockerfile.dev
      args:
        RUNNER_BASE_REPOSITORY: "${RUNNER_BASE_REPOSITORY:-ghcr.io/10gen/magenta-client-libraries/runner-base}"
        RUNNER_BASE_TAG: "${RUNNER_BASE_TAG:-0.1.28-alpha}\""""

new_text = text.replace(OLD, NEW)
if new_text == text:
    print("  (no exact matches — file may already be patched or pattern changed)")
else:
    path.write_text(new_text)
    print(f"  rewrote {path}")
PY

echo "✓ patch applied."
