# MergeGuard all-in-one container: API + worker.
#
# The orchestration + LLM paths are stdlib-only. The GitHub webhook
# integration needs PyJWT[crypto] for signing App JWTs (RS256), so we
# install that one Python dep — everything else stays stdlib.

FROM python:3.11-slim

WORKDIR /app

# Don't write .pyc files (avoids spurious file changes if the source is
# bind-mounted for hot iteration).
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# curl: healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# PyJWT[crypto] is required by packages/github_pr/app_client.py when the
# webhook handler resolves an installation token for a real GitHub App
# event. Without it, signed PR events fail with 500 "PyJWT is required".
RUN pip install --no-cache-dir "PyJWT[crypto]>=2.8"

# Copy only what the runtime needs. Keeping .agentic/, tools/, tests/ out
# of the image keeps it small and avoids inheriting agent-stack noise.
COPY apps ./apps
COPY packages ./packages
COPY agents ./agents
COPY fixtures ./fixtures
COPY data ./data

EXPOSE 4100

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:4100/ || exit 1

CMD ["python3", "apps/api/main.py"]
