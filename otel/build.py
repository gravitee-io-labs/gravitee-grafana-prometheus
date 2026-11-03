#!/usr/bin/env python3
"""Generates the Elasticsearch-free Gravitee dashboard pack into otel/grafana/dashboards/,
and the per-API SLO recording rules into otel/prometheus/rules/.

Data comes from the Gateway's OpenTelemetry signals, through an OpenTelemetry Collector:
- Prometheus: per-API metrics derived from the spans (gravitee_calls_total, gravitee_duration_milliseconds_*),
  plus the Gateway's own JVM / HTTP metrics;
- Tempo: the traces, one per call;
- Loki: the OTel logs, with the payloads of logged calls.
Every dashboard picks its data sources through variables (ds_prom, ds_loki, ds_tempo).
Run: python3 otel/build.py
"""
import copy, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dashboards-src"))
import build as es_pack  # the Elasticsearch pack: shared panel helpers, and the runtime dashboard reused below
from build import (Layout, C_OK, C_4XX, C_5XX, C_GW, C_BE, PROM, EXPR, STATUS_OVERRIDES, prom, reduce_, math, base,
                   row, ts, stat, bargauge, pie, table, text, ov, color_ov, unit_ov, cell_color, organize, ds_var,
                   dashboard)

OUT = os.path.join(HERE, "grafana", "dashboards")
RULES = os.path.join(HERE, "prometheus", "rules", "gravitee-otel-slo.rules.yml")
TAG = "gravitee-otel"
LOKI = {"type": "loki", "uid": "${ds_loki}"}
TEMPO = {"type": "tempo", "uid": "${ds_tempo}"}

# Metrics the collector's spanmetrics connector derives from the Gateway's spans
C, D = "gravitee_calls_total", "gravitee_duration_milliseconds"
API = 'gravitee_api_name=~"$api"'
SRV = f'span_kind="SPAN_KIND_SERVER", {API}'   # one per call, client side: status, method, application
CLI = f'span_kind="SPAN_KIND_CLIENT", {API}'   # one per backend call
# A rejection is on the policy span and on the request-phase span: count reasons on the phase span only
REJ = f'{API}, gravitee_execution_failure_key!="", gravitee_policy=""'
PLAN = f'{API}, span_kind="SPAN_KIND_INTERNAL", gravitee_execution_phase="request", flow=~"plan-.*"'
NAMED_APP = ('label_replace(label_replace({q}, "application", "$1", "gravitee_application_name", "(.+)"), '
             '"application", "(keyless / rejected)", "gravitee_application_name", "")')
# OTel logs: one record per hop of a logged call (entrypoint-request, endpoint-request, endpoint-response,
# entrypoint-response), with the API, application, request id and trace id as structured metadata
LOGS = '{service_name="gio-apim-gateway"} | gravitee_api_name=~"$api"'
CLIENT_RESP = LOGS + ' | gravitee_log_type="entrypoint-response"'


def inc(sel, rng="$__range"):
    return f"sum(increase({C}{{{sel}}}[{rng}]))"


def ratio(num_sel, den_sel=SRV):
    """Share of calls over the dashboard range; 0 when nothing matches the numerator."""
    return f"({inc(num_sel)} or vector(0)) / {inc(den_sel)}"


def p(q, sel=SRV, by="", rng="$__range"):
    b = f", {by}" if by else ""
    return f"histogram_quantile({q}, sum by (le{b}) (rate({D}_bucket{{{sel}}}[{rng}])))"


def avg_ms(sel, by="", rng="$__range"):
    b = f" by ({by})" if by else ""
    return f"sum{b} (rate({D}_sum{{{sel}}}[{rng}])) / sum{b} (rate({D}_count{{{sel}}}[{rng}]))"


def overhead(by="", rng="$__range"):
    # server and client spans of a call can fall in different scrapes: never below 0 on short windows
    return f"clamp_min({avg_ms(SRV, by, rng)} - {avg_ms(CLI, by, rng)}, 0)"


def rate_by(sel, by, rng="$__rate_interval"):
    return f"sum by ({by}) (rate({C}{{{sel}}}[{rng}]))"


def top(sel, by, rng="$__range"):
    return f"sort_desc(sum by ({by}) (increase({C}{{{sel}}}[{rng}])))"


def instant(ref, expr, legend=""):
    return prom(ref, expr, legend, instant=True)


def status_classes(sel=SRV):
    return [prom(r, f'sum(rate({C}{{{sel}, http_status_code=~"{c}.."}}[$__rate_interval]))', f"{c}xx")
            for r, c in zip("ABCD", "2345")]


def loki(ref, expr, legend="", instant=False):
    return {"refId": ref, "datasource": LOKI, "expr": expr, "legendFormat": legend,
            "queryType": "instant" if instant else "range"}


def tempo(ref, query, limit=50):
    return {"refId": ref, "datasource": TEMPO, "queryType": "traceql", "query": query, "limit": limit,
            "spss": 1, "tableType": "traces"}


def traceql(cond):
    return '{resource.gravitee.api.name=~"${api:regex}" && kind=server && ' + cond + "} with (most_recent=true)"


def display(p, label):
    """Name bars and slices by a label, also when there is a single one (Grafana hides the name then)."""
    p["fieldConfig"]["defaults"]["displayName"] = "${__field.labels.%s}" % label
    if p["fieldConfig"]["defaults"].get("unit") != "ms":
        p["fieldConfig"]["defaults"]["decimals"] = 0   # increase() extrapolates: show whole calls
    return p


# ---------------------------------------------------------------- links
DS_PARAMS = "${ds_prom:queryparam}&${ds_loki:queryparam}&${ds_tempo:queryparam}"
API_LINK = [{"title": "Open the drill-down of ${__field.labels.gravitee_api_name}${__data.fields.API}",
             "url": "/d/gravitee-otel-api?${__url_time_range}&" + DS_PARAMS +
                    "&var-api=${__field.labels.gravitee_api_name}${__data.fields.API}"}]
CALL_URL = "/d/gravitee-otel-logs?${__url_time_range}&" + DS_PARAMS + "&var-request_id=${__value.raw}"
TRACE_URL = ('/explore?schemaVersion=1&orgId=1&panes={"t":{"datasource":"${ds_tempo}","queries":[{"refId":"A",'
             '"datasource":{"type":"tempo","uid":"${ds_tempo}"},"queryType":"traceql","query":"${__value.raw}"}],'
             '"range":{"from":"${__from}","to":"${__to}"}}}')
LOG_COLS = {"Time": "Time", "gravitee_api_name": "API", "gravitee_application_name": "Application",
            "http_response_status_code": "Status", "gravitee_request_id": "Request id", "trace_id": "Trace",
            "Line": "Response body"}
LOG_LINKS = [ov("Request id", [{"id": "links", "value": [{"title": "Show this call's payloads", "url": CALL_URL}]}]),
             ov("Trace", [{"id": "links", "value": [{"title": "Open the trace", "url": TRACE_URL}]}]),
             cell_color("Status", [{"color": "green", "value": None}, {"color": "orange", "value": 400},
                                   {"color": "red", "value": 500}]),
             ov("Response body", [{"id": "custom.width", "value": 420}])]


TO_NUMBER = {"id": "convertFieldType", "options": {"conversions": [
    {"targetField": "http_response_status_code", "destinationType": "number"}]}}
NOT_CALLED = ov("Status", [{"id": "mappings", "value": [{"type": "value", "options": {
    "0": {"text": "not called", "color": "transparent", "index": 0}}}]}])


def log_table(L, title, query, cols=LOG_COLS, w=24, h=10, desc="", overrides=None):
    """Loki records as a table: one column per structured metadata field (API, application, request id…)."""
    names = list(cols)
    return table(L, title, [loki("A", query)], w=w, h=h, desc=desc,
                 transformations=[{"id": "extractFields", "options": {"source": "labels", "replace": False}},
                                  TO_NUMBER,
                                  {"id": "filterFieldsByName", "options": {"include": {"names": names}}},
                                  organize(cols, order=names)],
                 overrides=(overrides if overrides is not None else LOG_LINKS))


def trace_table(L, title, cond, w=12, h=9, desc=""):
    return table(L, title, [tempo("A", traceql(cond))], w=w, h=h, desc=desc, sort="Start time",
                 transformations=[organize(exclude=["traceService"],
                                           rename_map={"traceName": "Call", "startTime": "Start time",
                                                       "traceDuration": "Duration", "traceID": "Trace"})])


# ---------------------------------------------------------------- variables
def api_var(multi=True):
    q = f'label_values({C}{{span_kind="SPAN_KIND_SERVER"}}, gravitee_api_name)'
    v = {"name": "api", "label": "API", "type": "query", "datasource": PROM, "query": {"query": q, "refId": "api"},
         "definition": q, "refresh": 2, "sort": 1, "includeAll": multi, "multi": multi, "allValue": ".*",
         "current": {"text": "All", "value": "$__all"} if multi else {}}
    return v


def otel_vars(api=True, multi=True, loki_=True, tempo_=True):
    v = [ds_var("ds_prom", "Metrics (Prometheus)", "prometheus", "prometheus")]
    if loki_:
        v.append(ds_var("ds_loki", "Call logs (Loki)", "loki", "gravitee-loki"))
    if tempo_:
        v.append(ds_var("ds_tempo", "Traces (Tempo)", "tempo", "gravitee-tempo"))
    if api:
        v.append(api_var(multi))
    return v


def otel_dashboard(uid, title, desc, panels, variables, **kw):
    d = dashboard(uid, title, desc, panels, variables, **kw)
    d["tags"] = [TAG]
    d["links"][0]["tags"] = [TAG]
    return d


# ============================================================== 1. OVERVIEW
LAT_T = [{"color": "green", "value": None}, {"color": "orange", "value": 300}, {"color": "red", "value": 1000}]
GW_T = [{"color": "green", "value": None}, {"color": "orange", "value": 20}, {"color": "red", "value": 50}]
AVAIL_T = [{"color": "red", "value": None}, {"color": "orange", "value": 0.99}, {"color": "green", "value": 0.995}]


def overview():
    L = Layout()
    P = []
    P.append(row(L, "At a glance"))
    P.append(stat(L, "API calls", [instant("A", inc(SRV))], w=4, graph=False, color_mode="background",
                  thresholds=[{"color": "#1f60c4", "value": None}],
                  desc="API calls handled by the Gateway in the selected range, from its OpenTelemetry spans."))
    P.append(stat(L, "Availability (non-5xx)", [instant("A", f"1 - {ratio(SRV + ', http_status_code=~\"5..\"')}")], w=4,
                  unit="percentunit", decimals=2, graph=False, thresholds=AVAIL_T,
                  desc="Share of calls that did NOT end in a 5xx. 4xx are the consumer's fault and don't count."))
    P.append(stat(L, "Server errors (5xx)", [instant("A", ratio(SRV + ', http_status_code=~"5.."'))], w=4,
                  unit="percentunit", decimals=2, graph=False,
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.01},
                              {"color": "red", "value": 0.05}]))
    P.append(stat(L, "Client errors (4xx)", [instant("A", ratio(SRV + ', http_status_code=~"4.."'))], w=4,
                  unit="percentunit", decimals=2, graph=False,
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.1},
                              {"color": "red", "value": 0.25}]))
    P.append(stat(L, "P95 response time", [instant("A", p(0.95))], w=4, unit="ms", decimals=0, graph=False,
                  thresholds=LAT_T, desc="95th percentile of the time seen by the consumer, from the span histograms."))
    P.append(stat(L, "Gateway overhead (avg)", [instant("A", overhead())], w=4, unit="ms", decimals=1, graph=False,
                  thresholds=GW_T, desc="Average time spent inside Gravitee (policies, security, routing): server span "
                  "minus backend span. An average, because percentiles can't be subtracted."))
    P.append(stat(L, "Active APIs", [instant("A", f"count(sum by (gravitee_api_name) (increase({C}{{{SRV}}}[$__range])) > 0)")],
                  w=4, graph=False, desc="APIs that received at least one call in the range."))
    P.append(stat(L, "Active applications", [instant("A", f'count(sum by (gravitee_application_name) (increase({C}{{{SRV}, '
                                                          f'gravitee_application_name!=""}}[$__range])) > 0)')],
                  w=4, graph=False, desc="Subscribed applications that called an API (keyless traffic excluded)."))
    P.append(stat(L, "Rate-limited calls (429)", [instant("A", f'{inc(SRV + ", http_status_code=\"429\"")} or vector(0)')],
                  w=4, graph=False, color_mode="background", decimals=0,
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}],
                  desc="Calls rejected by rate-limit / quota policies: usually a consumer that needs a bigger plan."))
    P.append(stat(L, "Rejected credentials (401/403)", [instant("A", f'{inc(SRV + ", http_status_code=~\"40[13]\"")} or vector(0)')],
                  w=4, graph=False, color_mode="background", decimals=0,
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}],
                  desc="Calls refused by plan security (missing or invalid API key, JWT, OAuth2 token) or access control."))
    P.append(stat(L, "Logged calls", [loki("A", f"sum(count_over_time({CLIENT_RESP} [$__range]))", instant=True)], w=4,
                  graph=False, decimals=0, desc="Calls with OTel logs (payloads) in Loki, per each API's logging condition."))
    P.append(stat(L, "Gateway nodes up", [instant("A", 'count(up{job="gravitee"} == 1)')], w=4, graph=False,
                  color_mode="background", thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]))

    P.append(row(L, "Traffic"))
    P.append(ts(L, "Calls by status class", status_classes(), w=12, stack=True, bars=True, unit="reqps",
                overrides=STATUS_OVERRIDES, legend_calcs=("mean", "max")))
    P.append(ts(L, "Calls by API", [prom("A", rate_by(SRV, "gravitee_api_name"), "{{gravitee_api_name}}")], w=12,
                stack=True, unit="reqps", legend_calcs=("mean", "max"), desc="Click a series name to isolate it."))
    P.append(ts(L, "Server error rate by API (5xx %)", [
        prom("A", f'({rate_by(SRV + ", http_status_code=~\"5..\"", "gravitee_api_name", "1m")} or '
                  f'{rate_by(SRV, "gravitee_api_name", "1m")} * 0) / {rate_by(SRV, "gravitee_api_name", "1m")}',
             "{{gravitee_api_name}}")], w=12, unit="percentunit", legend_calcs=("mean", "max")))
    P.append(ts(L, "Gateway throughput by status code", [
        prom("A", rate_by(SRV, "http_status_code"), "{{http_status_code}}")], w=12, unit="reqps", stack=True,
        legend_calcs=("mean", "max"),
        overrides=[color_ov("/^2/", C_OK, "byRegexp"), color_ov("/^4/", C_4XX, "byRegexp"),
                   color_ov("/^5/", C_5XX, "byRegexp")]))

    P.append(row(L, "Latency attribution — gateway vs backend"))
    P.append(ts(L, "Response time percentiles (consumer view)", [
        prom(r, p(q, rng="$__rate_interval"), f"P{int(q * 100)}") for r, q in (("A", 0.5), ("B", 0.95), ("C", 0.99))],
        w=8, unit="ms", desc="End-to-end time observed by the consumer."))
    P.append(ts(L, "P95: total vs backend, and gateway overhead", [
        prom("A", p(0.95, rng="$__rate_interval"), "Total (P95)"),
        prom("B", p(0.95, CLI, rng="$__rate_interval"), "Backend (P95)"),
        prom("C", overhead(rng="$__rate_interval"), "Gateway overhead (avg)")], w=8, unit="ms",
        overrides=[color_ov("Total (P95)", "yellow"), color_ov("Backend (P95)", C_BE),
                   color_ov("Gateway overhead (avg)", C_GW)],
        desc="If 'Total' moves with 'Backend', the slowness is upstream. If it moves with the overhead, look at policies."))
    P.append(pie(L, "Where the time goes (avg)", [instant("A", avg_ms(CLI), "Backend"),
                                                  instant("B", overhead(), "Gateway overhead")], w=8, unit="ms",
                 overrides=[ov("A", [{"id": "displayName", "value": "Backend"},
                                     {"id": "color", "value": {"mode": "fixed", "fixedColor": C_BE}}], "byFrameRefID"),
                            ov("B", [{"id": "displayName", "value": "Gateway overhead"},
                                     {"id": "color", "value": {"mode": "fixed", "fixedColor": C_GW}}], "byFrameRefID")]))
    P.append(api_table(L, "Latency by API (ms)", [
        ("Calls", inc(SRV).replace("sum(", "sum by (gravitee_api_name) (", 1)),
        ("P50 total", p(0.5, by="gravitee_api_name")), ("P95 total", p(0.95, by="gravitee_api_name")),
        ("P99 total", p(0.99, by="gravitee_api_name")), ("P95 backend", p(0.95, CLI, by="gravitee_api_name")),
        ("Gateway overhead (avg)", overhead("gravitee_api_name"))],
        units={"Calls": "short"}, default_unit="ms",
        colors={"P95 total": LAT_T, "Gateway overhead (avg)": GW_T}, desc="Click an API name to open its drill-down."))

    P.append(row(L, "Leaderboards"))
    P.append(display(bargauge(L, "Top APIs by calls", [instant("A", top(SRV, "gravitee_api_name"))], w=8,
                              links=API_LINK), "gravitee_api_name"))
    P.append(display(bargauge(L, "Top APIs by server errors (5xx)", [
        instant("A", top(SRV + ', http_status_code=~"5.."', "gravitee_api_name"))], w=8, links=API_LINK,
        thresholds=[{"color": "orange", "value": None}, {"color": "red", "value": 50}]), "gravitee_api_name"))
    P.append(display(bargauge(L, "Top APIs by P95 response time", [instant("A", f"sort_desc({p(0.95, by='gravitee_api_name')})")],
                              w=8, unit="ms", thresholds=LAT_T, links=API_LINK), "gravitee_api_name"))
    P.append(display(bargauge(L, "Top applications by calls", [instant("A", f"sort_desc(sum by (application) "
                                                                            f"({NAMED_APP.format(q=f'increase({C}{{{SRV}}}[$__range])')}))")],
                              w=8, desc="'(keyless / rejected)': keyless plans, and calls rejected before an "
                                        "application was identified."), "application"))
    P.append(display(bargauge(L, "Top plans by calls", [instant("A", top(PLAN, "flow"))], w=8, desc=PLAN_DESC),
                     "flow"))
    P.append(display(bargauge(L, "Calls by HTTP method", [instant("A", top(SRV, "http_method"))], w=8), "http_method"))

    P.append(row(L, "Policy & security outcomes"))
    P.append(ts(L, "Gateway rejections by reason (failure key)", [
        prom("A", rate_by(REJ, "gravitee_execution_failure_key"), "{{gravitee_execution_failure_key}}")], w=12,
        bars=True, stack=True, unit="reqps", legend_calcs=("mean", "max"), desc=REJ_DESC))
    P.append(display(pie(L, "Rejections by reason", [instant("A", top(REJ, "gravitee_execution_failure_key"))], w=6),
                     "gravitee_execution_failure_key"))
    P.append(display(bargauge(L, "Rejections by policy", [
        instant("A", top(f'{API}, gravitee_execution_failure_key!="", gravitee_policy!=""', "gravitee_policy"))], w=6,
        thresholds=[{"color": "orange", "value": None}], desc="Which policy stopped the call."), "gravitee_policy"))
    P.append(display(bargauge(L, "Rate-limited (429) by application", [
        instant("A", f"sort_desc(sum by (application) ({NAMED_APP.format(q=f'increase({C}{{{SRV}, http_status_code=\"429\"}}[$__range])')}))")],
        w=12, h=6, thresholds=[{"color": "orange", "value": None}],
        desc="Who is hitting their plan limits: candidates for a plan upgrade."), "application"))
    P.append(status_table(L, "Credential & access failures by API", 'http_status_code=~"40[13]"', SRV))
    P.append(status_table(L, "Backend failures by API", 'http_status_code=~"5.."', CLI,
                          desc="Status codes returned by the backends (the Gateway's client spans)."))

    P.append(row(L, "Investigate (traces)"))
    P.append(trace_table(L, "Latest failed calls (5xx)", "span.http.status_code >= 500", desc=TRACE_DESC))
    P.append(trace_table(L, "Latest slow calls (> 1 s)", "duration > 1s", desc=TRACE_DESC))

    return otel_dashboard("gravitee-otel-overview", "Gravitee OTel · API Program Overview",
                          "Every API managed by Gravitee APIM, from its OpenTelemetry signals: traffic, availability, "
                          "latency attribution, consumers and policy outcomes. No Elasticsearch.", P, otel_vars())


PLAN_DESC = ("Plans are identified by their flow (spans carry no plan id), so a plan without a flow isn't counted. "
             "The name is the flow's name.")
REJ_DESC = ("Calls a policy refused or failed, by Gravitee failure key (RATE_LIMIT_TOO_MANY_REQUESTS, "
            "QUOTA_TOO_MANY_REQUESTS…). Calls refused by plan security (no or invalid key) have no failure key: "
            "see the 401/403 counts.")
TRACE_DESC = "Click a trace to open its spans (phases, plan flow, each policy, the backend call) and its logged payloads."


def api_table(L, title, cols, units=None, default_unit=None, colors=None, desc="", w=24, h=8):
    """One row per API, one column per instant query."""
    targets = [instant(chr(65 + i), q) for i, (_, q) in enumerate(cols)]
    rename = {f"Value #{chr(65 + i)}": name for i, (name, _) in enumerate(cols)}
    overrides = [ov("API", [{"id": "links", "value": API_LINK}])]
    for name, _ in cols:
        u = (units or {}).get(name, default_unit)
        if u:
            overrides.append(unit_ov(name, u))
    for name, steps in (colors or {}).items():
        overrides.append(cell_color(name, steps))
    return table(L, title, targets, w=w, h=h, desc=desc, sort=cols[0][0], ds=PROM,
                 transformations=[{"id": "merge", "options": {}},
                                  {"id": "filterFieldsByName", "options": {"include": {"pattern": "gravitee_api_name|Value.*"}}},
                                  organize({"gravitee_api_name": "API", **rename})],
                 overrides=overrides)


def status_table(L, title, cond, sel, desc=""):
    return table(L, title, [instant("A", f"sort_desc(sum by (gravitee_api_name, http_status_code) "
                                         f"(increase({C}{{{sel}, {cond}}}[$__range])) > 0)")], w=12, h=8, sort="Calls",
                 desc=desc, transformations=[{"id": "filterFieldsByName", "options": {"exclude": {"names": ["Time"]}}},
                                             organize({"gravitee_api_name": "API", "http_status_code": "Status",
                                                       "Value": "Calls"})],
                 overrides=[ov("API", [{"id": "links", "value": API_LINK}]), ov("Calls", [{"id": "decimals", "value": 0}])])


# ============================================================== 2. API DRILL-DOWN
def api_detail():
    L = Layout()
    P = []
    P.append(row(L, "API health"))
    P.append(stat(L, "Calls", [instant("A", inc(SRV))], w=4, graph=False, color_mode="background",
                  thresholds=[{"color": "#1f60c4", "value": None}]))
    P.append(stat(L, "Availability (non-5xx)", [instant("A", f"1 - {ratio(SRV + ', http_status_code=~\"5..\"')}")], w=4,
                  unit="percentunit", decimals=2, graph=False, thresholds=AVAIL_T))
    P.append(stat(L, "Error rate (4xx + 5xx)", [instant("A", ratio(SRV + ', http_status_code=~"[45].."'))], w=4,
                  unit="percentunit", decimals=2, graph=False,
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.05},
                              {"color": "red", "value": 0.15}]))
    P.append(stat(L, "P95 total", [instant("A", p(0.95))], w=4, unit="ms", decimals=0, graph=False, thresholds=LAT_T))
    P.append(stat(L, "P95 backend", [instant("A", p(0.95, CLI))], w=4, unit="ms", decimals=0, graph=False))
    P.append(stat(L, "Gateway overhead (avg)", [instant("A", overhead())], w=4, unit="ms", decimals=1, graph=False,
                  thresholds=GW_T))

    P.append(row(L, "Traffic & errors"))
    P.append(ts(L, "Calls by status class", status_classes(), w=12, stack=True, bars=True, unit="reqps",
                overrides=STATUS_OVERRIDES))
    P.append(ts(L, "Calls by status code", [prom("A", rate_by(SRV, "http_status_code"), "{{http_status_code}}")], w=12,
                stack=True, unit="reqps",
                overrides=[color_ov("/^2/", C_OK, "byRegexp"), color_ov("/^4/", C_4XX, "byRegexp"),
                           color_ov("/^5/", C_5XX, "byRegexp")]))
    P.append(ts(L, "Calls by HTTP method", [prom("A", rate_by(SRV, "http_method"), "{{http_method}}")], w=8,
                stack=True, unit="reqps"))
    P.append(ts(L, "Rejections by reason (failure key)", [
        prom("A", rate_by(REJ, "gravitee_execution_failure_key"), "{{gravitee_execution_failure_key}}")], w=8,
        bars=True, stack=True, unit="reqps", desc=REJ_DESC))
    P.append(ts(L, "Backend calls by status", [prom("A", rate_by(CLI, "http_status_code"), "{{http_status_code}}")],
                w=8, stack=True, unit="reqps", desc="What the backend answered (the Gateway's client spans).",
                overrides=[color_ov("/^2/", C_OK, "byRegexp"), color_ov("/^4/", C_4XX, "byRegexp"),
                           color_ov("/^5/", C_5XX, "byRegexp")]))

    P.append(row(L, "Latency deep-dive"))
    P.append(ts(L, "Latency breakdown", [
        prom("A", p(0.95, rng="$__rate_interval"), "Total (P95)"),
        prom("B", p(0.95, CLI, rng="$__rate_interval"), "Backend (P95)"),
        prom("C", p(0.5, rng="$__rate_interval"), "Total (P50)"),
        prom("D", overhead(rng="$__rate_interval"), "Gateway overhead (avg)")], w=12, unit="ms",
        overrides=[color_ov("Total (P95)", "yellow"), color_ov("Backend (P95)", C_BE),
                   color_ov("Gateway overhead (avg)", C_GW)]))
    hm = base(L, "heatmap", "Response time distribution", 12, 8, [
        dict(prom("A", f"sum by (le) (increase({D}_bucket{{{SRV}}}[$__rate_interval]))", "{{le}}"), format="heatmap")],
        desc="Calls per response-time bucket (the span histogram's buckets). Bands reveal bimodal behaviour: cache "
             "hits vs misses, fast vs slow backends.")
    hm["options"] = {"calculate": False, "yAxis": {"unit": "ms", "axisPlacement": "left"},
                     "color": {"mode": "scheme", "scheme": "Oranges", "steps": 64, "exponent": 0.5},
                     "cellGap": 1, "tooltip": {"mode": "single", "yHistogram": True}, "rowsFrame": {"layout": "le"}}
    P.append(hm)

    P.append(row(L, "Consumers of this API"))
    P.append(app_scorecard(L, "Applications", w=12, h=9))
    P[-1]["fieldConfig"]["overrides"].append(ov("APIs used", [{"id": "custom.hidden", "value": True}]))
    P.append(ts(L, "Calls by application", [prom("A", f"sum by (application) ({NAMED_APP.format(q=f'rate({C}{{{SRV}}}[$__rate_interval])')})",
                                                 "{{application}}")], w=12, stack=True, unit="reqps"))
    P.append(display(pie(L, "Calls by plan", [instant("A", top(PLAN, "flow"))], w=8, desc=PLAN_DESC), "flow"))
    P.append(display(pie(L, "Rejections by policy", [
        instant("A", top(f'{API}, gravitee_execution_failure_key!="", gravitee_policy!=""', "gravitee_policy"))], w=8),
        "gravitee_policy"))
    P.append(display(pie(L, "Calls by HTTP method", [instant("A", top(SRV, "http_method"))], w=8), "http_method"))

    P.append(row(L, "Investigate (traces)"))
    P.append(trace_table(L, "Latest failed calls (4xx/5xx)", "span.http.status_code >= 400", desc=TRACE_DESC, h=10))
    P.append(trace_table(L, "Latest slow calls (> 500 ms)", "duration > 500ms", desc=TRACE_DESC, h=10))

    P.append(row(L, "Call logs of this API"))
    P.append(log_table(L, "Logged failed calls", CLIENT_RESP + " | http_response_status_code >= 400", h=9,
                       desc="Failed calls of this API with OTel logs. Click a request id for its payloads, or a trace "
                            "for its spans. Empty when OTel logs aren't enabled on the API."))
    P.append(log_table(L, "Latest logged calls", CLIENT_RESP, h=9))

    d = otel_dashboard("gravitee-otel-api", "Gravitee OTel · API Drill-down",
                       "Everything about one API: health, status codes, latency, consumers, and the individual failed "
                       "or slow calls, down to their traces and payloads.", P, otel_vars(multi=False))
    d["links"].append({"title": "Call logs of this API", "type": "link", "icon": "doc", "keepTime": True,
                       "url": "/d/gravitee-otel-logs?${api:queryparam}&" + DS_PARAMS,
                       "tooltip": "Payloads of this API's logged calls"})
    return d


def app_scorecard(L, title, w=24, h=9):
    by = "application"
    sel = lambda extra="": f'increase({C}{{{SRV}{extra}}}[$__range])'
    cols = [("Calls", f"sum by ({by}) ({NAMED_APP.format(q=sel())})"),
            ("P95 ms", f"histogram_quantile(0.95, sum by (le, {by}) ({NAMED_APP.format(q=f'rate({D}_bucket{{{SRV}}}[$__range])')}))"),
            ("APIs used", f"count by ({by}) (sum by ({by}, gravitee_api_name) ({NAMED_APP.format(q=sel())}) > 0)"),
            ("4xx", f'sum by ({by}) ({NAMED_APP.format(q=sel(", http_status_code=~" + chr(34) + "4.." + chr(34)))})'),
            ("5xx", f'sum by ({by}) ({NAMED_APP.format(q=sel(", http_status_code=~" + chr(34) + "5.." + chr(34)))})'),
            ("429 (rate-limited)", f'sum by ({by}) ({NAMED_APP.format(q=sel(", http_status_code=" + chr(34) + "429" + chr(34)))})')]
    targets = [instant(chr(65 + i), q) for i, (_, q) in enumerate(cols)]
    rename = {f"Value #{chr(65 + i)}": name for i, (name, _) in enumerate(cols)}
    return table(L, title, targets, w=w, h=h, sort="Calls", ds=PROM,
                 transformations=[{"id": "merge", "options": {}},
                                  {"id": "filterFieldsByName", "options": {"include": {"pattern": f"{by}|Value.*"}}},
                                  organize({by: "Application", **rename})],
                 overrides=[unit_ov("P95 ms", "ms"),
                            ov("/^(Calls|4xx|5xx|429.*|APIs used)$/", [{"id": "decimals", "value": 0},
                                                                         {"id": "noValue", "value": "0"}], "byRegexp"),
                            cell_color("429 (rate-limited)", [{"color": "transparent", "value": None},
                                                              {"color": "orange", "value": 1}]),
                            cell_color("5xx", [{"color": "transparent", "value": None}, {"color": "red", "value": 1}])],
                 desc="One line per application: volume, quality of service received, breadth of API usage, "
                      "rate-limit pressure. '(keyless / rejected)' groups keyless plans and calls rejected before "
                      "an application was identified.")


# ============================================================== 3. CONSUMERS
def consumers():
    L = Layout()
    P = []
    P.append(row(L, "Applications"))
    P.append(stat(L, "Active applications", [instant("A", f'count(sum by (gravitee_application_name) (increase({C}{{{SRV}, '
                                                          f'gravitee_application_name!=""}}[$__range])) > 0)')],
                  w=6, graph=False))
    P.append(stat(L, "Keyless or rejected share", [instant("A", ratio(SRV + ', gravitee_application_name=""'))], w=6,
                  unit="percentunit", decimals=1, graph=False,
                  desc="Calls with no identified application: keyless plans, and calls rejected before plan selection."))
    P.append(stat(L, "Rate-limited calls (429)", [instant("A", f'{inc(SRV + ", http_status_code=\"429\"")} or vector(0)')],
                  w=6, graph=False, decimals=0, color_mode="background",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]))
    P.append(stat(L, "Plans in use", [instant("A", f"count(sum by (flow) (increase({C}{{{PLAN}}}[$__range])) > 0)")],
                  w=6, graph=False, desc=PLAN_DESC))
    P.append(app_scorecard(L, "Application scorecard"))
    P.append(ts(L, "Calls by application", [prom("A", f"sum by (application) ({NAMED_APP.format(q=f'rate({C}{{{SRV}}}[$__rate_interval])')})",
                                                 "{{application}}")], w=12, stack=True, unit="reqps"))
    P.append(ts(L, "Rate-limited calls by application", [
        prom("A", f"sum by (application) ({NAMED_APP.format(q=f'rate({C}{{{SRV}, http_status_code={chr(34)}429{chr(34)}}}[$__rate_interval])')})",
             "{{application}}")], w=12, bars=True, stack=True, unit="reqps"))

    P.append(row(L, "Plans"))
    P.append(table(L, "Plan usage", [
        instant("A", f"sort_desc(sum by (gravitee_api_name, flow) (increase({C}{{{PLAN}}}[$__range])))"),
        instant("B", f"histogram_quantile(0.95, sum by (le, gravitee_api_name, flow) (rate({D}_bucket{{{PLAN}}}[$__range])))")],
        w=14, h=9, sort="Calls", ds=PROM, desc=PLAN_DESC + " 'Flow P95' is the time spent in the plan's request flow.",
        transformations=[{"id": "merge", "options": {}},
                         {"id": "filterFieldsByName", "options": {"include": {"pattern": "gravitee_api_name|flow|Value.*"}}},
                         organize({"gravitee_api_name": "API", "flow": "Plan flow", "Value #A": "Calls",
                                   "Value #B": "Flow P95"})],
        overrides=[unit_ov("Flow P95", "ms"), ov("Calls", [{"id": "decimals", "value": 0}]),
                   ov("API", [{"id": "links", "value": API_LINK}])]))
    P.append(ts(L, "Calls by plan", [prom("A", rate_by(PLAN, "flow"), "{{flow}}")], w=10, h=9, stack=True,
                unit="reqps", desc=PLAN_DESC))

    P.append(row(L, "Applications × APIs"))
    P.append(table(L, "Who calls what", [
        instant("A", f"sort_desc(sum by (application, gravitee_api_name) ({NAMED_APP.format(q=f'increase({C}{{{SRV}}}[$__range])')}))")],
        w=24, h=10, sort="Calls",
        transformations=[{"id": "filterFieldsByName", "options": {"exclude": {"names": ["Time"]}}},
                         organize({"application": "Application", "gravitee_api_name": "API", "Value": "Calls"})],
        overrides=[ov("API", [{"id": "links", "value": API_LINK}]), ov("Calls", [{"id": "decimals", "value": 0}])]))
    return otel_dashboard("gravitee-otel-consumers", "Gravitee OTel · Consumers & Plans",
                          "Who consumes your APIs, how much, with what quality of service, and on which plans.",
                          P, otel_vars(loki_=False, tempo_=False))


# ============================================================== 4. GATEWAY RUNTIME (+ telemetry pipeline)
def runtime():
    """The Elasticsearch pack's runtime dashboard is Prometheus-only except node monitoring: reuse it without
    that, and add the health of the telemetry pipeline."""
    d = es_pack.runtime()
    mon = lambda p: any(t.get("datasource", {}).get("uid") == "${ds_monitor}" for t in p.get("targets", []))
    panels = []
    for p in d["panels"]:
        if p["type"] == "row" and p["title"].startswith("Node monitoring"):
            break
        if p["title"] == "JVM uptime":
            p = copy.deepcopy(p)
            # The Gateway publishes no uptime metric outside Elasticsearch node monitoring
            p.update(title="JVM threads (max node)", datasource=PROM,
                     targets=[instant("A", "max(jvm_threads_live_threads{instance=~\"$instance\"})")],
                     description="Live JVM threads on the busiest node. A steady climb points to blocked or leaked threads.")
            p["fieldConfig"]["defaults"].pop("unit", None)
        elif mon(p):
            continue
        panels.append(p)
    L = Layout()
    L.y = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels)
    L.next_id = max(p["id"] for p in panels) + 1
    col = "otelcol_"
    panels.append(row(L, "Telemetry pipeline (OpenTelemetry Collector)"))
    panels.append(stat(L, "Collector up", [instant("A", 'max(up{job="otel-collector"})')], w=4, graph=False,
                       color_mode="background", mappings=[{"type": "value", "options": {"1": {"text": "UP"}, "0": {"text": "DOWN"}}}],
                       thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]))
    panels.append(stat(L, "Spans received / s", [prom("A", f"sum(rate({col}receiver_accepted_spans[$__rate_interval]))")],
                       w=4, unit="short", decimals=0, calc=True, calc_value="lastNotNull"))
    panels.append(stat(L, "Log records received / s", [prom("A", f"sum(rate({col}receiver_accepted_log_records[$__rate_interval]))")],
                       w=4, unit="short", decimals=1, calc=True, calc_value="lastNotNull"))
    panels.append(stat(L, "Refused (spans + logs)", [instant("A", f"sum(increase({col}receiver_refused_spans[$__range])) + "
                                                                f"sum(increase({col}receiver_refused_log_records[$__range]))")],
                       w=4, graph=False, decimals=0, thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
                       desc="Data the collector couldn't accept, usually because it is overloaded."))
    panels.append(stat(L, "Export failures", [instant("A", f"sum(increase({col}exporter_send_failed_spans[$__range])) + "
                                                         f"sum(increase({col}exporter_send_failed_log_records[$__range])) + "
                                                         f"sum(increase({col}exporter_send_failed_metric_points[$__range]))")],
                       w=4, graph=False, decimals=0, thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
                       desc="Spans, logs or metric points the collector failed to send to Tempo, Loki or Prometheus."))
    panels.append(stat(L, "Span metric series", [instant("A", f'count({{__name__=~"{C}|{D}_count"}})')], w=4,
                       graph=False, desc="Active series created by the span metrics. Grows with APIs × applications × "
                                         "status codes: watch it when adding labels."))
    panels.append(ts(L, "Received by signal", [
        prom("A", f"sum(rate({col}receiver_accepted_spans[$__rate_interval]))", "spans"),
        prom("B", f"sum(rate({col}receiver_accepted_log_records[$__rate_interval]))", "log records")], w=12, unit="short"))
    panels.append(ts(L, "Sent and failed by exporter", [
        prom("A", f"sum by (exporter) (rate({{__name__=~\"{col}exporter_sent_.*\"}}[$__rate_interval]))", "{{exporter}} sent"),
        prom("B", f"sum by (exporter) (rate({{__name__=~\"{col}exporter_send_failed_.*\"}}[$__rate_interval]))",
             "{{exporter}} failed")], w=12, unit="short",
        overrides=[color_ov("/failed/", C_5XX, "byRegexp")]))
    d["panels"] = panels
    d["templating"]["list"] = [v for v in d["templating"]["list"] if v["name"] in ("ds_prom", "instance")]
    d.update(uid="gravitee-otel-runtime", title="Gravitee OTel · Gateway Fleet & Runtime", tags=[TAG],
             description="Gateway nodes, inbound and outbound HTTP traffic, JVM memory and GC, CPU, and the health of "
                         "the OpenTelemetry pipeline that feeds the other dashboards.")
    d["links"][0]["tags"] = [TAG]
    return d


# ============================================================== 5. SLO
SLO_HELP = """
**Objectives:** 99.5 % of calls succeed (non-5xx) over 30 days → **error budget = 0.5 %**; calls answered within **500 ms**.

* **SLIs and burn rates, per API and overall,** are Prometheus *recording rules* on the span metrics
  (`prometheus/rules/gravitee-otel-slo.rules.yml`): cheap to evaluate, alertable.
* **Alerts** use the multi-window, multi-burn-rate pattern, **per API**:
  *page* at 14.4× (5m & 1h), *ticket* at 6× (30m & 6h).

Change the objectives in `otel/build.py` and rebuild.
"""


def slo():
    L = Layout()
    P = []
    P.append(row(L, "Service level objectives (all APIs)"))
    P.append(text(L, "How SLOs are computed", SLO_HELP, w=8, h=8))
    P.append(stat(L, "Availability SLI (30d)", [instant("A", "gravitee_otel:sli_availability:ratio_rate30d")], w=4, h=8,
                  unit="percentunit", decimals=3, graph=False,
                  thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 0.995}]))
    P.append(stat(L, "Error budget remaining", [instant("A", "gravitee_otel:error_budget_remaining:ratio30d")], w=4, h=8,
                  unit="percentunit", decimals=1, graph=False,
                  thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.25},
                              {"color": "green", "value": 0.5}]))
    P.append(stat(L, "Latency SLI: calls ≤ 500 ms (30d)", [instant("A", "gravitee_otel:sli_latency:ratio_rate30d")], w=4,
                  h=8, unit="percentunit", decimals=2, graph=False,
                  thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.95},
                              {"color": "green", "value": 0.99}]))
    P.append(stat(L, "Firing alerts", [instant("A", 'count(ALERTS{alertstate="firing", alertname=~"Gravitee.*"}) or vector(0)')],
                  w=4, h=8, graph=False, color_mode="background",
                  thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
                  desc="Alerts firing from prometheus/rules/gravitee-otel-alerts.rules.yml."))
    P.append(ts(L, "Error budget burn rate", [prom("A", "gravitee_otel:error_burn_rate:5m", "5m"),
                                              prom("B", "gravitee_otel:error_burn_rate:1h", "1h"),
                                              prom("C", "gravitee_otel:error_burn_rate:6h", "6h")], w=12, h=8,
                overrides=[ov("5m", [{"id": "custom.lineWidth", "value": 1}])],
                desc="1 = consuming the budget exactly at the allowed pace."))
    P.append(ts(L, "Availability SLI (rolling windows)", [
        prom("A", "gravitee_otel:sli_availability:ratio_rate5m", "5m"),
        prom("B", "gravitee_otel:sli_availability:ratio_rate1h", "1h"),
        prom("C", "vector(0.995)", "objective 99.5%")], w=12, h=8, unit="percentunit",
        overrides=[ov("objective 99.5%", [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                          {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                                          {"id": "custom.fillOpacity", "value": 0}])]))

    P.append(row(L, "Per-API SLO compliance"))
    P.append(api_table(L, "Per-API SLIs", [
        ("Calls", inc(SRV).replace("sum(", "sum by (gravitee_api_name) (", 1)),
        ("Availability (30d)", f"max by (gravitee_api_name) (gravitee_api:sli_availability:ratio_rate30d{{{API}}})"),
        ("Error budget left (30d)", f"max by (gravitee_api_name) (gravitee_api:error_budget_remaining:ratio30d{{{API}}})"),
        ("Burn rate (1h)", f"max by (gravitee_api_name) (gravitee_api:error_burn_rate:1h{{{API}}})"),
        ("≤ 500 ms (30d)", f"max by (gravitee_api_name) (gravitee_api:sli_latency:ratio_rate30d{{{API}}})"),
        ("P95 ms", p(0.95, by="gravitee_api_name"))],
        units={"Calls": "short", "Availability (30d)": "percentunit", "Error budget left (30d)": "percentunit",
               "≤ 500 ms (30d)": "percentunit", "P95 ms": "ms", "Burn rate (1h)": "none"},
        colors={"Availability (30d)": AVAIL_T,
                "Error budget left (30d)": [{"color": "red", "value": None}, {"color": "orange", "value": 0.25},
                                            {"color": "green", "value": 0.5}],
                "Burn rate (1h)": [{"color": "green", "value": None}, {"color": "orange", "value": 1},
                                   {"color": "red", "value": 6}],
                "P95 ms": LAT_T}, h=9,
        desc="Calls and P95 over the selected range; the other columns from the recording rules. A burn rate above 1 "
             "consumes the error budget faster than allowed."))
    P.append(ts(L, "Availability by API (1h)", [prom("A", f"gravitee_api:sli_availability:ratio_rate1h{{{API}}}",
                                                     "{{gravitee_api_name}}")], w=12, unit="percentunit",
                legend_calcs=("mean", "min")))
    P.append(ts(L, "Burn rate by API (1h)", [prom("A", f"gravitee_api:error_burn_rate:1h{{{API}}}",
                                                  "{{gravitee_api_name}}")], w=12, legend_calcs=("mean", "max"),
                desc="Above 14.4 together with the 5m burn: the fast-burn alert fires for that API."))
    for pn in P[-2:]:
        pn["interval"] = "1m"
    return otel_dashboard("gravitee-otel-slo", "Gravitee OTel · SLOs & Error Budgets",
                          "Availability and latency objectives per API, with multi-window burn-rate alerting, from the "
                          "Gateway's span metrics.", P, otel_vars(loki_=False, tempo_=False), time_from="now-24h",
                          refresh="1m")


# ============================================================== 6. CALL LOGS
LOGS_HELP = """
**Call logs** here are the Gateway's **OpenTelemetry logs**: the request and response **payloads** of each logged call,
on the client side and the backend side, linked to the call's trace. They're written for APIs with **OTel logs enabled**
(API → *Reporter settings*), for the calls that match the API's logging condition: the demo logs **1 call in 5**.
Headers are not exported as OTel logs: open the trace for the call's path through the Gateway.
**Search** filters payloads with a regular expression, e.g. `EUR` or `wayne`. ⚠️ Payloads can contain personal data:
restrict access to the Loki data source.
"""
HOPS = {"entrypoint-request": "1. Client → Gateway (request)", "endpoint-request": "2. Gateway → backend (request)",
        "endpoint-response": "3. Backend → Gateway (response)", "entrypoint-response": "4. Gateway → client (response)"}
ONE_CALL = '{service_name="gio-apim-gateway"} | gravitee_request_id="$request_id"'


def logs():
    L = Layout()
    P = []
    P.append(text(L, "", LOGS_HELP, h=4))
    P.append(row(L, "Logged calls"))
    P.append(stat(L, "Logged calls", [loki("A", f"sum(count_over_time({CLIENT_RESP} [$__range]))", instant=True)], w=6,
                  graph=False, decimals=0, color_mode="background", thresholds=[{"color": "#1f60c4", "value": None}]))
    P.append(stat(L, "Share of calls logged", [
        dict(loki("A", f"sum(count_over_time({CLIENT_RESP} [$__range]))", instant=True), hide=True),
        dict(instant("B", inc(SRV)), hide=True), dict(reduce_("C", "A"), hide=True), dict(reduce_("D", "B"), hide=True),
        math("E", "$C / $D")], w=6, unit="percentunit", decimals=1, graph=False,
        desc="Logged calls (Loki) / all calls (span metrics). Logging conditions and APIs without OTel logs keep "
             "this below 100 %."))
    P.append(stat(L, "Logged failures (4xx/5xx)", [
        loki("A", f"sum(count_over_time({CLIENT_RESP} | http_response_status_code >= 400 [$__range]))", instant=True)],
        w=6, graph=False, decimals=0, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]))
    P.append(stat(L, "Answered by the Gateway", [
        loki("A", f'sum(count_over_time({LOGS} | gravitee_log_type="endpoint-response" | http_response_status_code = 0 [$__range]))',
             instant=True)], w=6, graph=False, decimals=0,
        desc="Logged calls the Gateway answered without calling the backend (backend status 0): rejected by plan "
             "security or a policy such as rate limiting."))
    P.append(ts(L, "Logged calls by client status", [
        loki(r, f'sum(count_over_time({CLIENT_RESP} | http_response_status_code >= {lo} | http_response_status_code < {lo + 100} [$__auto]))', a)
        for r, lo, a in (("A", 200, "2xx"), ("B", 300, "3xx"), ("C", 400, "4xx"), ("D", 500, "5xx"))],
        w=12, stack=True, bars=True, overrides=STATUS_OVERRIDES, legend_calcs=("sum",)))
    P.append(display(bargauge(L, "Logged calls by API", [
        loki("A", f"sort_desc(sum by (gravitee_api_name) (count_over_time({CLIENT_RESP} [$__range])))", instant=True)],
        w=6), "gravitee_api_name"))
    P.append(display(bargauge(L, "Logged failures by status", [
        loki("A", f"sort_desc(sum by (http_response_status_code) (count_over_time({CLIENT_RESP} | http_response_status_code >= 400 [$__range])))",
             instant=True)], w=6, thresholds=[{"color": "orange", "value": None}]), "http_response_status_code"))

    P.append(row(L, "Call log explorer"))
    P.append(log_table(L, "Logged failed calls", CLIENT_RESP + " | http_response_status_code >= 400", h=9,
                       desc="Click a request id to show its payloads below, or a trace to open its spans."))
    P.append(log_table(L, "Payload search", LOGS + ' |~ "(?i)${search:raw}"', h=9,
                       cols={"Time": "Time", "gravitee_log_type": "Hop", "gravitee_api_name": "API",
                             "gravitee_application_name": "Application", "gravitee_request_id": "Request id",
                             "trace_id": "Trace", "Line": "Payload"},
                       overrides=LOG_LINKS[:2] + [ov("Payload", [{"id": "custom.width", "value": 520}])],
                       desc="Every logged payload (any hop) that matches Search. Click a request id to show the whole call."))

    P.append(row(L, "Call detail"))
    # A call's records are written in hop order: sort on their time, then drop it
    P.append(table(L, "Call summary", [loki("A", ONE_CALL)], w=10, h=14,
                   transformations=[{"id": "extractFields", "options": {"source": "labels", "replace": False}}, TO_NUMBER,
                                    {"id": "sortBy", "options": {"sort": [{"field": "Time", "desc": False}]}},
                                    {"id": "filterFieldsByName", "options": {"include": {"names": [
                                        "gravitee_log_type", "http_request_method", "http_url",
                                        "http_response_status_code", "trace_id"]}}},
                                    organize({"gravitee_log_type": "Hop", "http_request_method": "Method",
                                              "http_url": "URL", "http_response_status_code": "Status",
                                              "trace_id": "Trace"},
                                             order=["gravitee_log_type", "http_request_method", "http_url",
                                                    "http_response_status_code", "trace_id"])],
                   overrides=[LOG_LINKS[1], LOG_LINKS[2], NOT_CALLED,
                              ov("Hop", [{"id": "custom.width", "value": 230}, {"id": "mappings", "value": [
                                  {"type": "value", "options": {k: {"text": v} for k, v in HOPS.items()}}]}])],
                   desc="The hops of the call selected by Request id: method and URL on requests, status on responses. "
                        "Click the trace for the time spent in each policy."))
    P.append(table(L, "Payloads", [loki("A", ONE_CALL)], w=14, h=14,
                   transformations=[{"id": "extractFields", "options": {"source": "labels", "replace": False}},
                                    {"id": "sortBy", "options": {"sort": [{"field": "Time", "desc": False}]}},
                                    {"id": "filterFieldsByName", "options": {"include": {"names": ["gravitee_log_type", "Line"]}}},
                                    organize({"gravitee_log_type": "Hop", "Line": "Body"}, order=["gravitee_log_type", "Line"])],
                   overrides=[ov("Hop", [{"id": "custom.width", "value": 230},
                                         {"id": "mappings", "value": [{"type": "value", "options": {
                                             k: {"text": v} for k, v in HOPS.items()}}]}]),
                              ov("Body", [{"id": "custom.cellOptions", "value": {"type": "auto", "wrapText": True}}])],
                   desc="Bodies at each hop. Comparing 1↔2 or 3↔4 shows what policies changed (transformation, masking)."))
    v = otel_vars()
    v.append({"name": "request_id", "label": "Request id (call detail)", "type": "textbox", "query": "",
              "current": {"text": "", "value": ""}, "hide": 0})
    v.append({"name": "search", "label": "Search payloads (regex)", "type": "textbox", "query": "",
              "current": {"text": "", "value": ""}, "hide": 0})
    return otel_dashboard("gravitee-otel-logs", "Gravitee OTel · Call Logs",
                          "Payloads of logged API calls, from the Gateway's OpenTelemetry logs in Loki: search them, "
                          "list failed calls, and inspect one call hop by hop.", P, v, time_from="now-1h")


# ============================================================== SLO recording rules
def slo_rules():
    S = 'span_kind="SPAN_KIND_SERVER"'
    out = ["# Per-API availability and latency SLIs, and error-budget burn rates, from the metrics the OpenTelemetry",
           "# Collector derives from the Gateway's spans. Objective: 99.5% non-5xx over 30 days (error budget 0.005);",
           "# latency objective: answered within 500 ms. Generated by otel/build.py: edit it there.",
           "groups:", "  - name: gravitee-otel-sli", "    interval: 30s", "    rules:"]

    def rule(name, expr):
        out.append(f"      - record: {name}\n        expr: {expr}")
    for by, prefix in (("gravitee_api_name", "gravitee_api"), ("", "gravitee_otel")):
        g = f" by ({by})" if by else ""
        rule(f"{prefix}:calls:rate5m", f"sum{g} (rate({C}{{{S}}}[5m]))")
        for w in ("5m", "30m", "1h", "6h", "30d"):
            tot = f"sum{g} (rate({C}{{{S}}}[{w}]))"
            err = f'sum{g} (rate({C}{{{S}, http_status_code=~"5.."}}[{w}]))'
            rule(f"{prefix}:sli_availability:ratio_rate{w}", f"1 - (({err}) or ({tot} * 0)) / {tot}")
        for w in ("5m", "1h", "30d"):
            # Prometheus 3 stores bucket boundaries as floats (le="500.0")
            rule(f"{prefix}:sli_latency:ratio_rate{w}",
                 f'sum{g} (rate({D}_bucket{{{S}, le=~"500(\\\\.0)?"}}[{w}])) / sum{g} (rate({D}_count{{{S}}}[{w}]))')
    out += ["", "  - name: gravitee-otel-burn", "    interval: 30s", "    rules:"]
    for prefix in ("gravitee_api", "gravitee_otel"):
        for w in ("5m", "30m", "1h", "6h"):
            rule(f"{prefix}:error_burn_rate:{w}", f"(1 - {prefix}:sli_availability:ratio_rate{w}) / 0.005")
        rule(f"{prefix}:error_budget_remaining:ratio30d", f"1 - (1 - {prefix}:sli_availability:ratio_rate30d) / 0.005")
    return "\n".join(out) + "\n"


def main():
    os.makedirs(OUT, exist_ok=True)
    for name in os.listdir(OUT):
        if name.endswith(".json"):
            os.remove(os.path.join(OUT, name))
    for fn in (overview, api_detail, consumers, runtime, slo, logs):
        d = fn()
        with open(os.path.join(OUT, f"{d['uid']}.json"), "w") as f:
            json.dump(d, f, indent=2)
        print(f"{d['uid']:26} {sum(1 for p in d['panels'] if p['type'] not in ('row', 'text')):3} panels")
    with open(RULES, "w") as f:
        f.write(slo_rules())


if __name__ == "__main__":
    main()
