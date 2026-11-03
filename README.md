# Gravitee APIM — Grafana dashboard pack + demo stack

A Gravitee APIM 4.13 stack with Prometheus and Grafana. It comes with **six linked dashboards**, SLO recording rules and alerts, and a seeded set of APIs with continuous realistic traffic, so every panel shows data.

It started from the Gravitee tutorial [Monitor APIM with Prometheus and Grafana](https://documentation.gravitee.io/apim/how-to-guides/use-case-tutorials/monitor-apim-with-prometheus-and-grafana), with its bugs fixed (see below). Ports are remapped so it can run next to another local Gravitee stack on the default ports.

```
consumers ──► Gateway ──► backend (httpbin)
                 │  ├─ internal API :18082 /_node/metrics/prometheus ──► Prometheus (+ SLO rules, alerts) ─┐
                 │  ├─ analytics reporter ──► Elasticsearch gravitee-v4-metrics-*  (one record per call) ──┼─► Grafana
                 │  ├─ node monitoring   ──► Elasticsearch gravitee-monitor-*      (JVM / CPU per node) ──┤
                 │  └─ call logging      ──► Elasticsearch gravitee-v4-log-*       (headers + payloads) ──┘
```

## Quick start

```bash
docker compose up -d                       # ~1-2 min until the gateway is up
python3 scripts/seed.py                    # 4 APIs, 5 plans, 5 applications, 9 subscriptions, call logging
docker compose --profile demo up -d        # continuous traffic generator (~15 req/s)
```

Open Grafana at http://localhost:3000 (admin / admin). The home page is **Gravitee · API Program Overview**; the other dashboards are in the *Gravitee dashboards* menu.

### Enterprise license (optional, local only)

Put `gravitee_license_key=<base64 license>` in `.secrets/license.env`. The gateway and the Management API load it through `env_file` (`required: false`, so the stack still starts without it). The `.secrets/` folder is git-ignored and never copied into the pack, and `dashboards-src/package.sh` refuses to build if license material appears in its output.

With a license, rebuild the demo APIs with the Assign Metrics stamps (`custom.tenant`, `custom.plan`, `custom.channel`, `custom.app`):

```bash
ENTERPRISE=1 python3 scripts/seed.py --recreate && docker compose --profile demo restart traffic
```

Without one, those panels explain how to enable them.

## Dashboards

| Dashboard | Panels | Highlights |
| --- | --- | --- |
| API Program Overview | 35 | KPIs, status classes, gateway vs backend latency split, leaderboards, rejections by error key and by policy, 429 by application, world map |
| API Drill-down | 23 | Per-path latency/errors, connect time + TTFB, latency heatmap, consumers, latest failed and slowest calls, and the API's logged calls, each opening its headers and payloads |
| Consumers, Plans & Tenants | 17 | Application scorecard, plan usage, subscriptions, custom business dimensions, geography, user agents |
| Gateway Fleet & Runtime | 30 | Node table, inbound/outbound HTTP, backend connection pool, heap/GC/threads/buffers, CPU/load/FDs, node monitoring without Prometheus |
| SLOs & Error Budgets | 10 | 30-day availability SLI, error budget remaining, multi-window burn rates, per-API SLO compliance table |
| Call Logs | 16 | Logged calls and failures, calls answered by the Gateway, searchable call list with client vs backend status, one call's summary, payloads and headers at each hop (credential headers hidden) |

The dashboards are **generated**: edit `dashboards-src/build.py`, then run `python3 dashboards-src/build.py`. Grafana reloads them within a few seconds.

**Download the ready-made pack** from the [latest release](https://github.com/gravitee-io-labs/gravitee-grafana-prometheus/releases/latest) (`gravitee-grafana-dashboards.zip`, open `GUIDE.html` first). **To build it yourself**, run `dashboards-src/package.sh`. It produces `dist/gravitee-grafana-dashboards.zip`: the dashboards, data source and dashboard provisioning files, Prometheus rules, and an install guide (`dashboards-src/INSTALL.md`). Every dashboard picks its data sources through dropdowns, so it imports into any Grafana.

## Without Elasticsearch: the OpenTelemetry edition

[`otel/`](otel/) is a separate pack and demo stack for teams that don't run Elasticsearch. The Gateway sends OpenTelemetry traces and logs to an OpenTelemetry Collector, which derives per-API metrics from the spans (Prometheus) and stores the traces (Tempo) and logged payloads (Loki). Six dashboards mirror the ones above, with per-API SLO rules and alerts, a trace behind every call, and the health of the telemetry pipeline.

```bash
cd otel && docker compose up -d                                          # own ports: Grafana :3001, Gateway :38082
OTEL=1 MGMT_URL=http://localhost:38083/management python3 ../scripts/seed.py
docker compose --profile demo up -d                                      # traffic
```

Open Grafana at http://localhost:3001 (admin / admin). It runs next to the Elasticsearch stack. Build the pack with `otel/package.sh` (`dist/gravitee-grafana-dashboards-otel.zip`); [otel/INSTALL.md](otel/INSTALL.md) covers the setup on an existing installation and what this edition can't show (geography, user agents, per-path tables, headers, plan ids).

## Screenshots & video

`media/` holds one full-height PNG per dashboard, a world-map hero image, and a 1080p walkthrough video (`gravitee-dashboards-walkthrough.mp4`, ~1 min, captioned). To regenerate them from the live stack (needs Google Chrome and ffmpeg):

```bash
(cd scripts && npm i --no-save playwright-core@1.48.2)
FROM=now-1h node scripts/capture-media.js all     # or: shots | video; FROM also takes an epoch-ms start
ONLY=gravitee-logs node scripts/capture-media.js shots   # just some dashboards
```

More history gives smoother charts and SLO numbers that reflect the baseline rather than the latest incident.

## URLs

| Service            | URL                                | Credentials   |
| ------------------ | ---------------------------------- | ------------- |
| Grafana            | http://localhost:3000              | admin / admin |
| Prometheus         | http://localhost:9090 (Alerts tab) | –             |
| Management Console | http://localhost:28084             | admin / admin |
| Management API     | http://localhost:28083             | admin / admin |
| Gateway            | http://localhost:28082             | –             |

Ports, versions, passwords and the traffic rate (`TRAFFIC_RPS`) are set in [.env](.env). Everything binds to `127.0.0.1`.

## Demo data

`scripts/seed.py` (idempotent) creates:

| API | Path | Plans |
| --- | --- | --- |
| Payments API | `/payments` | Gold (API key, 200 req/s), Free (API key, 3 req/s) |
| Orders API | `/orders` | Partner (API key, 50 req/s) |
| Customer Profile API | `/customers` | Internal (API key, 100 req/s) |
| Product Catalog API | `/catalog` | Public (keyless, 30 req/s) |

It also creates the applications Mobile Banking App, Partner Portal, Retail Web, Internal BI and Hackathon Sandbox, with their subscriptions. API keys are written to `scripts/.keys.json` (gitignored).

Every API logs headers and payloads, client and backend side, for **1 call in 5** (condition `{#request.id.hashCode() % 5 == 0}`; the condition runs before the response, so it can't select failed calls). That's about 0.5 GB of logs a day at the default traffic rate.

`scripts/traffic.py` (the `traffic` service, or run it locally with `python3 scripts/traffic.py [seconds]`) mixes:
- applications, 12 countries (via `X-Forwarded-For` → geo-IP) and real user agents;
- a healthy baseline of about 0.3% 5xx, plus 4xx, slow calls, and bad or missing API keys;
- Free-plan bursts that trip the rate limit;
- **a backend incident on one API every ~10 minutes**, lasting 2 minutes, which drives the burn-rate alerts.

## Layout

```
docker-compose.yml, .env
prometheus/prometheus.yml                 scrape job "gravitee" -> gateway:18082
prometheus/rules/gravitee-slo.rules.yml   SLI + burn-rate recording rules (objective 99.5%)
prometheus/rules/gravitee-alerts.rules.yml gateway down, fast/slow burn, latency, heap, GC, CPU, backend 5xx
grafana/provisioning/                     data sources (Prometheus, Gravitee Analytics, Gravitee Node Monitoring, Gravitee Call Logs)
grafana/dashboards/                       generated dashboards (do not edit by hand)
dashboards-src/build.py                   dashboard generator
dashboards-src/package.sh, INSTALL.md     distributable pack
scripts/seed.py, scripts/traffic.py       demo estate + traffic (OTEL=1: traces and OTel logs on every API)
otel/                                     OpenTelemetry edition: stack, collector, generator (build.py), pack (package.sh)
```

## Fixes compared with the tutorial

| Tutorial | Problem | Here |
| --- | --- | --- |
| `gravitee_management_mongodb_uri` | Not read by 4.13, so the gateway kept dialing `localhost:27017` | `gravitee_ds_mongodb_host=mongodb` |
| Internal API left on `localhost` | Prometheus can't scrape it from another container | `gravitee_services_core_http_host=0.0.0.0` |
| Raw `http_server_*` queries | They also count Prometheus's own scrapes on `:18082` | Filtered on `local=~".*:8082"` |
| Dashboard pasted by hand, legacy panels | The import doesn't ask for a data source; the panel type is deprecated | Provisioned, data source variables, `timeseries` panels |
| Error-rate query | Returns no data when there are no 5xx | `or vector(0)` |
| `curl localhost:8082` | No API exists, so every call is a 404 | Seeded APIs + traffic generator |
| Unpinned images, fixed names/ports | Not reproducible; clashes with other stacks | Pinned in `.env`, remapped ports |

## Known gateway metric quirks (APIM 4.13.5)

- `http_server_active_requests` does not decrement for some rejected calls: it was seen climbing to ~500 while only 2 connections were open. It is not charted.
- `http_client_*` metrics have no `remote` label, so backend metrics are per gateway node, not per backend. Per-backend detail comes from the analytics `endpoint` field.
- The Prometheus response-time timer is a summary without histogram buckets. Percentiles and "share of calls under X ms" therefore come from Elasticsearch, which records every call.

## Stop / reset

```bash
docker compose --profile demo down        # keep data
docker compose --profile demo down -v     # also delete all volumes
```

## License

[Apache License 2.0](LICENSE).
