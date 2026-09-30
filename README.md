# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

## Observability

`docker compose up` also starts an OpenTelemetry Collector, Prometheus, Loki, Tempo, and Grafana. The app exports traces, metrics, and logs via OTLP to the Collector, which fans traces out to Tempo, metrics to Prometheus, and logs to Loki. Config lives under `observability/`.

Open Grafana at <http://127.0.0.1:3000> (default login `admin` / `admin`) for the pre-provisioned "Order Tracker - Requests" dashboard, which shows request counts and error rates by route and status code. Prometheus, Loki, and Tempo are also pre-wired as Grafana datasources.

A provisioned Grafana alert rule ("Order Tracker - 5xx errors by route", in the "Order Tracker" folder) fires per route when 5xx responses occur in the last 5 minutes. It links back to the dashboard panel, and treats a quiet period (no 5xx at all) as healthy rather than "no data".

## Incident response

`incident-response/` is a second FastAPI service, built and started by the same `docker compose up`. Grafana's 5xx alert routes to it (via the "incident-response" contact point) as a webhook at `POST /alerts` on port 8001.

On a firing alert it:
1. Saves the alert payload, recent logs (Loki), and recent traces (Tempo) for the affected endpoint under `incidents/<timestamp>-<alert>/` (a Docker volume).
2. Runs the GitHub Copilot CLI headlessly (`copilot -p ... --allow-all-tools`) against the incident report and the repo (mounted read-write at `/workspace`) to investigate and propose or apply a fix. Output is captured in that same incident folder (`assistant.log`, `session.md`).

Notes:
- Requires a `GH_TOKEN` (or `GITHUB_TOKEN`) with Copilot access in the environment for the assistant step to actually run; without it, the incident report is still captured but the assistant step fails fast with an auth error in `assistant.log`. A token is required even if you're already signed in to Copilot in VS Code/via SSO on the host — that session's credential lives in the OS credential store (e.g. Windows Credential Manager), not in a file, so it can't be mounted into the container. The Copilot CLI accepts an OAuth token (e.g. from `gh auth login` + `gh auth token`) or a **fine-grained** personal access token (classic tokens aren't accepted); authorize it for SSO if your org/enterprise enforces it. Put it in a local, gitignored `.env` file at the repo root (`GH_TOKEN=...`) so `docker compose up` picks it up automatically, instead of exporting it every session.
- The webhook requires HTTP Basic auth (`ALERT_WEBHOOK_USERNAME` / `ALERT_WEBHOOK_PASSWORD`, default `grafana` / `order-tracker-alerts` — change these for anything beyond local use).
- Set `AUTO_INVOKE_ASSISTANT=false` to only capture incident context without ever launching the assistant. Repeated alerts for the same issue are throttled by `ASSISTANT_COOLDOWN_SECONDS` (default 10 minutes).
