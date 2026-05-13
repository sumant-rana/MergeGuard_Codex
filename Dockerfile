# MergeGuard all-in-one container: API + worker, dependency-light.
#
# The code is stdlib-only on the runtime path, so we don't need uv / pip;
# we just COPY the source and run apps/api/main.py. The worker is
# triggered via the API's /api/demo/analyze endpoint, or one-shot via
# `python3 apps/worker/main.py` inside the container.

FROM python:3.11-slim

WORKDIR /app

# Don't write .pyc files (avoids spurious file changes if the source is
# bind-mounted for hot iteration).
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# curl is used by the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

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
