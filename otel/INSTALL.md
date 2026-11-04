# Gravitee APIM — Grafana dashboard pack, OpenTelemetry edition (no Elasticsearch)

Six linked dashboards for monitoring Gravitee API Management **without Elasticsearch**. The Gateway sends OpenTelemetry traces and logs to an OpenTelemetry Collector. The collector derives per-API metrics from the spans for Prometheus, and stores the traces in Tempo and the logged payloads in Loki.

```
Gateway ─ OTLP traces + logs ─► OpenTelemetry Collector ─┬─ metrics derived from spans ─► Prometheus
        │                                                ├─ traces                      ─► Tempo
        │                                                └─ OTel logs (payloads)        ─► Loki
        └─ /_node/metrics/prometheus (JVM, CPU, HTTP) ──────────────────────────────────► Prometheus
```

| Dashboard | Answers | Data sources |
| --- | --- | --- |
| **API Program Overview** | How are all my APIs doing? Who calls them, and what does the Gateway reject and why? | Prometheus, Loki, Tempo |
| **API Drill-down** | What is wrong with *this* API: which consumer, which call, and what did it send and receive? | Prometheus, Tempo, Loki |
| **Consumers & Plans** | Which applications and plans drive traffic, with what quality of service, and who hits their limits? | Prometheus |
| **Gateway Fleet & Runtime** | Are the Gateway nodes and the telemetry pipeline healthy: JVM, GC, CPU, connections, collector? | Prometheus |
| **SLOs & Error Budgets** | Is each API meeting 99.5% availability and 500 ms, and how fast is its budget burning? | Prometheus |
| **Call Logs** | What exactly did this call send and receive, on the client side and on the backend side? | Loki, Prometheus |

Every call can be opened as a **trace**: its phases, plan flow, each policy with its duration, and the backend call.

## Requirements

| Component | Version / setting |
| --- | --- |
| Gravitee APIM | 4.13 or later, v4 APIs. The OpenTelemetry reporter (`gravitee-reporter-otel`) ships in the 4.13 Gateway image. Tested on 4.13.5. |
| OpenTelemetry Collector | *contrib* distribution (for the `spanmetrics` connector), tested on 0.115 |
| Prometheus | Tested on 3.5 |
| Tempo | 2.7 or later: the trace tables use the `most_recent` search hint (tested on 2.7.2) |
| Loki | 3.0 or later, with structured metadata (tested on 3.3) |
| Grafana | Tested on 12.2 |

## Install

1. **Gateway** (`gravitee.yml`, or the matching `gravitee_*` environment variables / Helm values):

   ```yaml
   services:
     opentelemetry:
       enabled: true
       exporter:
         endpoint: http://otel-collector:4317           # traces, gRPC
         protocol: grpc
         logsEndpoint: http://otel-collector:4318/v1/logs   # OTel logs, always HTTP
     metrics:                                            # JVM, CPU and HTTP totals for Prometheus
       enabled: true
       prometheus:
         enabled: true
   reporters:
     otel:
       enabled: true
     elasticsearch:
       enabled: false
   ```

   On the **Management API**, set `repositories.analytics.type: none` to run without an analytics store (`gravitee_repositories_analytics_type=none`).

2. **Each API**: turn on tracing, and OTel logs where you want payloads. In the Console, open the API's reporter settings (*Trace enabled*, *OTel Logs*). With the Management API, set the `analytics` block of the v4 API:

   ```json
   "analytics": { "enabled": true,
                  "tracing": { "enabled": true, "verbose": false },
                  "otelLogs": { "enabled": true },
                  "logging": { "mode": { "entrypoint": true, "endpoint": true },
                               "phase": { "request": true, "response": true },
                               "content": { "payload": true },
                               "condition": "{#request.id.hashCode() % 5 == 0}" } }
   ```

   Tracing must be on for every call: the per-API metrics are computed from the spans. OTel logs follow the API's `logging` settings and condition.

3. **OpenTelemetry Collector**: use `collector/collector.yaml`. It derives `gravitee_calls_total` and `gravitee_duration_milliseconds` from the spans, exposes them on `:8889`, and sends traces to Tempo and logs to Loki's OTLP endpoint. Adjust the Tempo and Loki addresses.

4. **Prometheus**: add the scrape jobs in `prometheus-rules/scrape-config.example.yml` (the Gateway's internal API, the collector's span metrics on `:8889` and its own health on `:8888`), and load `prometheus-rules/*.yml` (`rule_files:`). They provide per-API SLO recording rules and alerts: per-API fast and slow error-budget burn, per-API latency, Gateway down, heap, GC, CPU, and telemetry pipeline failures.

5. **Tempo and Loki**: `collector/tempo.yaml` and `collector/loki.yaml` are single-process examples. In Loki, allow structured metadata (`allow_structured_metadata: true`, schema v13).

6. **Grafana**: add the Prometheus, Tempo and Loki data sources (`grafana-provisioning/datasources/datasources.yml` links traces to logs and back), then import `dashboards/` or provision the folder. Each dashboard picks its data sources through dropdowns.

## What differs from the Elasticsearch edition

The OpenTelemetry signals don't carry everything the Elasticsearch analytics record does:

| Not charted here | Why | Closest alternative |
| --- | --- | --- |
| Country, city, world map | No geo-IP on spans | The client IP is on each trace |
| User agents, client OS | The span has the raw user agent, but as a metric label it would create too many series | Search traces in Tempo by `span.user_agent.original` |
| Per-path traffic and latency table | The path is on each span, but as a label it would create too many series | Search traces by `span.http.target` |
| Plan names and ids | Spans carry no plan id | Plans appear through their **flow name** (`plan-gold plan`); a plan without a flow isn't counted |
| Business dimensions (`custom.*`, Assign Metrics) | Not exported on spans | — |
| Request and response headers in Call Logs | OTel logs carry bodies only | The trace shows the call's path through the Gateway |
| Reason for plan-security rejections (401) | No failure key on those spans | The 401/403 counts |
| Payload sizes | On the server span (`http.request.body.size`, `http.response.body.size`, from `Content-Length`), but not charted | Shown on each trace |
| Backend connect time, time-to-first-byte | Not on spans | Backend time from the client span |

The Gateway overhead is an **average** (server span minus backend span): percentiles can't be subtracted.

## Sizing

- **Traces**: about 13 spans per call (non-verbose). All calls are traced, since the metrics come from them. Size Tempo's retention accordingly.
- **Metric series**: they grow with APIs × applications × status codes × methods. The demo (4 APIs, 5 applications) creates about 430 series. The *Gateway Fleet & Runtime* dashboard shows the current count. Every label added in `collector.yaml` multiplies it.
- **Call logs**: four records per logged call (one per hop). Keep the logging condition selective and Loki's retention short.

## Tuning

- **SLO objective**: 99.5% and 500 ms by default. Edit `otel/build.py` (`slo_rules()`) in the source repository and rebuild, or edit `0.005` and `le="500"` in `gravitee-otel-slo.rules.yml`.
- **Thresholds**: latency colours switch at 300 ms / 1 s; gateway overhead at 20 ms / 50 ms.

## Notes

- **Personal data**: OTel logs contain payloads, and traces contain client IPs and URLs. Restrict access to the Loki and Tempo data sources.
- **Attribute names**: the Gateway uses the older OpenTelemetry HTTP attribute names (`http.status_code`, `http.method`). If a later APIM version moves to `http.response.status_code`, update the dimensions in `collector.yaml`.
- **Short histories**: error budgets are computed over 30 days. With only a few hours of data, a single incident can show a large negative budget.

## Source and license

Source, demo stack and new releases: https://github.com/gravitee-io-labs/gravitee-grafana-prometheus (folder `otel/`). Licensed under the Apache License 2.0 (`LICENSE`).
