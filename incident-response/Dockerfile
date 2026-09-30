FROM node:22-slim

# node:22-slim has no CA bundle; the copilot binary's own TLS client needs one
# to validate GitHub's certificate (Node's bundled fetch doesn't, which is why
# only Node-based checks appeared to work here).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The GitHub Copilot CLI drives the headless investigation once an incident is captured.
RUN npm install -g @github/copilot@1.0.89

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
ENV INCIDENTS_DIR=/data/incidents
EXPOSE 8001
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
