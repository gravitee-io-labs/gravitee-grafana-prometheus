# Gravitee APIM — Grafana dashboard pack

> **Start with `GUIDE.html`.** Open it in a browser for the full guide: a video tour, screenshots, what each dashboard answers, step-by-step setup, how to interact with the dashboards, alerts, customisation and troubleshooting. This file is the short reference.

Six linked dashboards for monitoring Gravitee API Management, from the executive view down to the headers and payloads of a single call.

| Dashboard | Answers | Data source |
| --- | --- | --- |
| **API Program Overview** | How are all my APIs doing? Who calls them, from where, and what does the gateway reject and why? | Analytics + Prometheus |
| **API Drill-down** | What is wrong with *this* API: which path, which consumer, which call, and what did it send and receive? | Analytics + call logs |
| **Consumers, Plans & Tenants** | Which applications and plans drive traffic, with what quality of service, and who hits their limits? | Analytics |
| **Gateway Fleet & Runtime** | Are the gateway nodes healthy: JVM, GC, CPU, connections, backend pool? | Prometheus + node monitoring |
| **SLOs & Error Budgets** | Are we meeting 99.5% availability, how fast is the budget burning, and which API is breaching? | Prometheus + Analytics |
| **Call Logs** | What exactly did this call send and receive, on the client side and on the backend side? | Call logs (optional) |

## What you get without extra configuration

Gravitee writes one analytics record per API call. These dashboards use it as-is:

- **API, application, plan and subscription** on every call, so there's no per-key or per-path exporter to configure.
- **Latency attribution** for every call: total, backend, *gateway overhead*, backend connect time and time-to-first-byte.
- **Why a call was rejected**: the Gravitee error key (e.g. `RATE_LIMIT_TOO_MANY_REQUESTS`, `GATEWAY_PLAN_UNRESOLVABLE`, `JWT_INVALID_TOKEN`) and the policy that rejected it.
- **Geo-IP** (country, city, coordinates) and **user-agent** parsing, shown on a world map and in client breakdowns.
- **The raw call records** (request id, URI, status, timings), so you can go from a chart to the individual failed or slow calls, and from there to their headers and payloads when API logging is on.

## Requirements

A few panels use data available only with Gravitee Enterprise, such as the business dimensions set by the Assign Metrics policy. Without a license, those panels explain how to enable them.

| Component | Version / setting |
| --- | --- |
| Gravitee APIM | 4.x, v4 APIs (analytics index `gravitee-v4-metrics-*`) |
| Analytics reporter | Elasticsearch or OpenSearch (the default APIM reporter) |
| Gateway metrics | `services.metrics.enabled=true`, `services.metrics.prometheus.enabled=true` |
| Grafana | Tested on 12.2 |
| Prometheus | Tested on 3.5 |

## Install

1. **Add the data sources** in Grafana. The names are free, and you pick them in each dashboard's dropdowns. Ready-made provisioning files are in `grafana-provisioning/`.
   - Elasticsearch, index `gravitee-v4-metrics-*`, time field `@timestamp` (*API analytics*)
   - Elasticsearch, index `gravitee-monitor-*`, time field `@timestamp` (*node monitoring*)
   - Prometheus, scraping the gateway at `http://<gateway>:18082/_node/metrics/prometheus` (basic auth)
   - Optional: Elasticsearch, index `gravitee-v4-log-*`, time field `@timestamp` (*call logs*, for the Call Logs dashboard)
2. **Import the dashboards** from `dashboards/` (Dashboards → New → Import → Upload JSON), or provision the folder. They are linked to each other through the *Gravitee dashboards* menu, and API names in tables open the drill-down.
3. **Load the Prometheus rules** from `prometheus-rules/` (`rule_files:` in `prometheus.yml`). They provide the SLO recording rules that the SLO dashboard needs, plus alerts: gateway down, fast/slow error-budget burn, latency, heap, GC, CPU and backend errors. Route the alerts with Alertmanager.

## Call logs (optional)

The Call Logs dashboard reads the logs Gravitee writes for APIs with logging enabled: **API → Logs → Configure Reporting**, or the API's `analytics.logging` block in the Management API:

```json
"logging": { "mode": { "entrypoint": true, "endpoint": true },
             "phase": { "request": true, "response": true },
             "content": { "headers": true, "payload": true },
             "condition": "{#request.headers['X-Debug'] != null}" }
```

- The condition is evaluated when the request arrives, so it can't select on the response status. Log a sample, a debug header or a time window (`#request.timestamp`).
- A logged call takes about 2 KB, several times its analytics record. Keep the log index's retention short.
- Logs can contain personal data and credentials. The dashboard hides only the `X-Gravitee-Api-Key`, `Authorization`, `Proxy-Authorization`, `Cookie` and `Set-Cookie` headers. API keys sent under another header name, and credentials inside bodies, stay visible, and everything is stored in the index: restrict access to the call-logs data source, and add your own header names to the Call Logs panels if needed.
- Search payloads with wildcards (`*ORD-1234*`): bodies are split on spaces only. Headers are stored but not searchable.

## Add your own business dimensions (Enterprise)

Stamp any value onto the analytics record with the **Assign Metrics** policy. It shows up as `custom.<name>`, and the *Consumers, Plans & Tenants* dashboard already has panels for `custom.tenant`, `custom.plan` and `custom.channel`:

```json
{ "policy": "policy-assign-metrics",
  "configuration": { "scope": "REQUEST", "metrics": [
    { "name": "tenant",
      "value": "{#request.headers['X-Tenant-ID'] != null ? #request.headers['X-Tenant-ID'][0] : 'unknown'}" } ] } }
```

Always give the expression a fallback value: a null result drops the metric. Put the policy on each plan's flow with a literal value (`"value": "Gold"`) to get readable plan names.

## Tuning

- **SLO objective**: 99.5% by default. Change `0.005` in `gravitee-slo.rules.yml` and in the *Per-API availability* table transformation.
- **Thresholds**: latency colours switch at 300 ms / 1 s; gateway overhead at 20 ms / 50 ms. Edit them in the panel's *Thresholds*.
- **Filter on any field**: every analytics panel honours the *Filter* bar. Try `application-name.keyword`, `plan-id`, `status` or `geoip.country_iso_code`.

## Source and license

Source, demo stack and new releases: https://github.com/gravitee-io-labs/gravitee-grafana-prometheus. Licensed under the Apache License 2.0 (`LICENSE`).

## Notes

- Prometheus series are labelled `local` (the listening address). The dashboards keep `:8082` (API traffic) and exclude `:18082`, the internal API that Prometheus itself scrapes.
- `http_server_active_requests` is not charted. On APIM 4.13.5 it does not decrement for some rejected calls, so it overstates the load.
- v2 (legacy) APIs report to the `gravitee-request-*` and `gravitee-log-*` indices with a different schema and are not covered.
