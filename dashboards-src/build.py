#!/usr/bin/env python3
"""Generates the Gravitee Grafana dashboard pack into grafana/dashboards/.

Every dashboard selects its data sources through variables (ds_analytics, ds_monitor, ds_prom),
so the same JSON works when provisioned here and when imported into any other Grafana.
Run: python3 dashboards-src/build.py
"""
import copy, json, os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "grafana", "dashboards")
TAG = "gravitee"

ES = {"type": "elasticsearch", "uid": "${ds_analytics}"}
MON = {"type": "elasticsearch", "uid": "${ds_monitor}"}
LOG = {"type": "elasticsearch", "uid": "${ds_logs}"}
PROM = {"type": "prometheus", "uid": "${ds_prom}"}
EXPR = {"type": "__expr__", "uid": "__expr__"}

# Gravitee stores the HTTP method as an enum code
METHOD_MAP = [{"type": "value", "options": {str(k): {"text": v} for k, v in
               {0: "OTHER", 1: "CONNECT", 2: "DELETE", 3: "GET", 4: "HEAD", 5: "OPTIONS",
                6: "PATCH", 7: "POST", 8: "PUT", 9: "TRACE"}.items()}}]
APP = "application-name.keyword"   # populated natively since APIM 4.x for subscribed applications
NO_APP = "(keyless / rejected)"
APP_MAP = []

# Palette: status classes keep a fixed meaning everywhere
C_OK, C_3XX, C_4XX, C_5XX, C_GW, C_BE = "green", "blue", "orange", "red", "purple", "blue"

# Lucene filter shared by every analytics query: API + application + plan selectors
# Lucene filter shared by every analytics query. Only api-name is always present: filtering other fields
# with an "All" value of "*" would silently drop rejected calls (no plan / application), so those are
# filtered with the ad hoc "Filter" variable instead.
F = 'api-name:$api'
GW_PORT = 'local=~".*:8082"'   # API traffic only (excludes the :18082 internal/scrape API)


# ---------------------------------------------------------------- query helpers
def _bucket_terms(field, size=10, order_by="_count", bid="3", missing=None):
    s = {"size": str(size), "order": "desc", "orderBy": order_by, "min_doc_count": "1"}
    if missing:
        s["missing"] = missing
    return {"id": bid, "type": "terms", "field": field, "settings": s}


def _dh(bid="2"):
    return {"id": bid, "type": "date_histogram", "field": "@timestamp",
            "settings": {"interval": "auto", "min_doc_count": "0", "trimEdges": "0"}}


def es(ref, query=F, metrics=None, terms=None, ts=True, alias="", ds=ES, size=10, order_by="_count", hide=False,
       missing=None):
    """Elasticsearch query. terms: field or list of fields to group by; ts: add a date histogram."""
    buckets = []
    for i, t in enumerate([terms] if isinstance(terms, str) else (terms or [])):
        buckets.append(_bucket_terms(t, size=size, order_by=order_by, bid=str(10 + i),
                                     missing=missing if t == APP else None))
    if ts:
        buckets.append(_dh())
    if not buckets and not any(m["type"] == "raw_data" for m in (metrics or [])):
        # Range-wide single value: one bucket per environment (per node for the monitoring index, per API for
        # the log index, which has no environment field); stats sum the buckets
        field = "hostname" if ds is MON else "api-id" if ds is LOG else "env-id"
        buckets.append(_bucket_terms(field, size=100 if ds is LOG else 10, bid="9"))
    return {"refId": ref, "datasource": ds, "query": query, "alias": alias, "hide": hide,
            "metrics": metrics or [count()], "bucketAggs": buckets, "timeField": "@timestamp"}


def count(mid="1"):
    return {"id": mid, "type": "count"}


def pct(field, percents=("95",), mid="1"):
    return {"id": mid, "type": "percentiles", "field": field, "settings": {"percents": list(percents)}}


def avg(field, mid="1"):
    return {"id": mid, "type": "avg", "field": field}


def sum_(field, mid="1"):
    return {"id": mid, "type": "sum", "field": field}


def max_(field, mid="1"):
    return {"id": mid, "type": "max", "field": field}


def card(field, mid="1"):
    return {"id": mid, "type": "cardinality", "field": field}


def raw(size=200):
    return {"id": "1", "type": "raw_data", "settings": {"size": str(size)}}


def prom(ref, expr, legend="", instant=False, hide=False):
    q = {"refId": ref, "datasource": PROM, "expr": expr, "legendFormat": legend, "hide": hide, "range": not instant}
    if instant:
        q["instant"] = True
        q["format"] = "table"
    return q


def reduce_(ref, of, reducer="sum"):
    return {"refId": ref, "datasource": EXPR, "type": "reduce", "expression": of, "reducer": reducer,
            "settings": {"mode": "replaceNN", "replaceWithValue": 0}}


def math(ref, expression):
    return {"refId": ref, "datasource": EXPR, "type": "math", "expression": expression}


def ratio_stat(num_query, den_query=F):
    """Stat value = count(num_query) / count(den_query) over the dashboard range."""
    return [es("A", num_query, hide=True), es("B", den_query, hide=True),
            dict(reduce_("C", "A"), hide=True), dict(reduce_("D", "B"), hide=True), math("E", "$C / $D")]


# ---------------------------------------------------------------- panel helpers
class Layout:
    def __init__(self):
        self.y, self.x, self.row_h, self.next_id = 0, 0, 0, 1

    def place(self, w, h):
        if self.x + w > 24:
            self.y += self.row_h
            self.x, self.row_h = 0, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def newline(self):
        if self.x:
            self.y += self.row_h
        self.x, self.row_h = 0, 0

    def nid(self):
        self.next_id += 1
        return self.next_id


def base(L, ptype, title, w, h, targets, desc="", unit=None, ds=None, decimals=None, mappings=None,
         overrides=None, thresholds=None, color=None, links=None, transformations=None, options=None,
         custom=None, minv=None, maxv=None):
    defaults = {"custom": custom or {}, "mappings": mappings or []}
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
    if color:
        defaults["color"] = color
    if links:
        defaults["links"] = links
    if minv is not None:
        defaults["min"] = minv
    if maxv is not None:
        defaults["max"] = maxv
    ds = ds or (targets[0]["datasource"] if targets and targets[0]["datasource"]["type"] != "__expr__" else ES)
    uses_expr = any(t["datasource"]["type"] == "__expr__" for t in targets)
    return {"id": L.nid(), "type": ptype, "title": title, "description": desc,
            "datasource": {"type": "datasource", "uid": "-- Mixed --"} if uses_expr else ds,
            "gridPos": L.place(w, h), "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": options or {}, "transformations": transformations or []}


def row(L, title, collapsed=False):
    L.newline()
    p = {"id": L.nid(), "type": "row", "title": title, "collapsed": collapsed,
         "gridPos": {"x": 0, "y": L.y, "w": 24, "h": 1}, "panels": []}
    L.y += 1
    return p


def ts(L, title, targets, w=12, h=8, desc="", unit="short", stack=False, bars=False, overrides=None,
       legend_calcs=("mean", "max"), fill=12, **kw):
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 2, "fillOpacity": 80 if bars else fill,
              "gradientMode": "opacity", "showPoints": "never", "spanNulls": True, "lineInterpolation": "smooth",
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}, "axisSoftMin": 0}
    return base(L, "timeseries", title, w, h, targets, desc, unit, custom=custom, overrides=overrides,
                options={"legend": {"displayMode": "table", "placement": "bottom", "calcs": list(legend_calcs),
                                    "sortBy": legend_calcs[0].capitalize() if legend_calcs else None, "sortDesc": True},
                         "tooltip": {"mode": "multi", "sort": "desc"}}, **kw)


def stat(L, title, targets, w=4, h=4, desc="", unit="short", thresholds=None, decimals=None, color_mode="value",
         graph=True, text_size=None, **kw):
    opts = {"reduceOptions": {"calcs": ["sum"], "fields": "", "values": False}, "colorMode": color_mode,
            "graphMode": "area" if graph else "none", "justifyMode": "center", "textMode": "value_and_name"
            if kw.pop("show_name", False) else "value", "wideLayout": True}
    if kw.pop("calc", None):
        opts["reduceOptions"]["calcs"] = [kw.pop("calc_value")]
    if text_size:
        opts["text"] = {"valueSize": text_size}
    return base(L, "stat", title, w, h, targets, desc, unit, decimals=decimals, options=opts,
                thresholds=thresholds or [{"color": "blue", "value": None}], **kw)


def bargauge(L, title, targets, w=8, h=9, desc="", unit="short", thresholds=None, links=None, **kw):
    return base(L, "bargauge", title, w, h, targets, desc, unit, links=links,
                thresholds=thresholds or [{"color": "blue", "value": None}],
                color={"mode": "continuous-BlYlRd"} if kw.pop("heat", False) else {"mode": "thresholds"},
                options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                         "valueMode": "color", "namePlacement": "left", "sizing": "manual",
                         "maxVizHeight": 26, "minVizHeight": 16, "minVizWidth": 8,
                         "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True}}, **kw)


def pie(L, title, targets, w=6, h=9, desc="", unit="short", overrides=None, **kw):
    return base(L, "piechart", title, w, h, targets, desc, unit, overrides=overrides,
                options={"pieType": "donut", "displayLabels": ["percent"],
                         "legend": {"displayMode": "table", "placement": "right", "values": ["value", "percent"]},
                         "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True},
                         "tooltip": {"mode": "single"}}, **kw)


def table(L, title, targets, w=12, h=9, desc="", overrides=None, transformations=None, sort=None, unit=None, **kw):
    opts = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}
    if sort:
        opts["sortBy"] = [{"displayName": sort, "desc": True}]
    return base(L, "table", title, w, h, targets, desc, unit, overrides=overrides,
                transformations=transformations, options=opts,
                custom={"align": "auto", "cellOptions": {"type": "auto"}, "inspect": True, "filterable": True}, **kw)


def text(L, title, content, w=24, h=6):
    return {"id": L.nid(), "type": "text", "title": title, "gridPos": L.place(w, h),
            "options": {"mode": "markdown", "content": content}}


def ov(name, props, by="byName"):
    return {"matcher": {"id": by, "options": name}, "properties": props}


def color_ov(name, color, by="byName"):
    return ov(name, [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}], by)


def unit_ov(name, unit, by="byName"):
    return ov(name, [{"id": "unit", "value": unit}], by)


def cell_color(name, steps, mode="color-background"):
    return ov(name, [{"id": "custom.cellOptions", "value": {"type": mode, "mode": "gradient"}},
                     {"id": "thresholds", "value": {"mode": "absolute", "steps": steps}}])


def join(field):
    """Outer-join several ES table frames on their group-by field (merge would key on Count too)."""
    return {"id": "joinByField", "options": {"byField": field, "mode": "outer"}}


def rename(mapping):
    return {"id": "organize", "options": {"renameByName": mapping, "excludeByName": {}, "indexByName": {}}}


def organize(rename_map=None, exclude=(), order=None):
    return {"id": "organize", "options": {"renameByName": rename_map or {},
                                          "excludeByName": {e: True for e in exclude},
                                          "indexByName": {n: i for i, n in enumerate(order or [])}}}


PLAN_DESC = ("Plan names come from the Assign Metrics stamp (custom.plan, Enterprise). Calls without a stamp are "
             "grouped by plan id.")


def plan_targets():
    """Readable plan names when stamped (custom.plan), plan ids for the rest — works on both editions."""
    return [es("A", F + " AND _exists_:custom.plan", terms="custom.plan", ts=False),
            es("B", F + " AND NOT _exists_:custom.plan AND _exists_:plan-id", terms="plan-id", ts=False)]


STATUS_OVERRIDES = [color_ov("2xx", C_OK), color_ov("3xx", C_3XX), color_ov("4xx", C_4XX), color_ov("5xx", C_5XX)]


def status_class_targets(extra=""):
    q = F + extra
    return [es("A", q + " AND status:[200 TO 299]", alias="2xx"), es("B", q + " AND status:[300 TO 399]", alias="3xx"),
            es("C", q + " AND status:[400 TO 499]", alias="4xx"), es("D", q + " AND status:[500 TO 599]", alias="5xx")]


API_LINK = [{"title": "Open ${__field.labels.api-name}${__data.fields[\"api-name\"]} drill-down",
             "url": "/d/gravitee-api-detail?${__url_time_range}&${ds_analytics:queryparam}&${ds_prom:queryparam}"
                    "&var-api=${__data.fields[\"api-name\"]}"}]


# Request id column → that call's headers and payloads in the Call Logs dashboard
CALL_LOG_LINK = ov("request-id", [{"id": "links", "value": [{
    "title": "Open the call log (headers & payloads), if this call was logged",
    "url": "/d/gravitee-logs?${__url_time_range}&${ds_analytics:queryparam}&${ds_logs:queryparam}"
           "&var-request_id=${__value.raw}&var-search=*"}]}])


# ---------------------------------------------------------------- variables
def ds_var(name, label, dstype, default_uid):
    # No name regex: any data source of the type can be picked. Defaults to the uids provisioned with this
    # pack; when absent (imported elsewhere) Grafana falls back to the first data source of that type.
    return {"name": name, "label": label, "type": "datasource", "query": dstype, "regex": "",
            "current": {"text": default_uid, "value": default_uid}, "hide": 0, "refresh": 1,
            "includeAll": False, "multi": False}


def es_terms_var(name, label, field, multi=True, all_value="*", query="*", hide=0, ds=ES):
    return {"name": name, "label": label, "type": "query", "datasource": ds,
            "query": json.dumps({"find": "terms", "field": field, "query": query, "size": 500}),
            "definition": f"terms {field}", "refresh": 2, "sort": 1, "includeAll": True, "multi": multi,
            "allValue": all_value, "current": {"text": "All", "value": "$__all"}, "hide": hide}


def common_vars(api=True, adhoc=True, monitor=False, prometheus=True):
    v = [ds_var("ds_analytics", "Analytics (Elasticsearch)", "elasticsearch", "gravitee-analytics")]
    if monitor:
        v.append(ds_var("ds_monitor", "Node monitoring (Elasticsearch)", "elasticsearch", "gravitee-monitor"))
    if prometheus:
        v.append(ds_var("ds_prom", "Prometheus", "prometheus", "prometheus"))
    if api:
        v.append(es_terms_var("api", "API", "api-name"))
    if adhoc:
        # Filters every analytics query on any field: application-name, plan-id, status, geoip.country_iso_code…
        v.append({"name": "filters", "label": "Filter (any field)", "type": "adhoc", "datasource": ES})
    return v


def dashboard(uid, title, desc, panels, variables, time_from="now-1h", refresh="30s"):
    return {
        "uid": uid, "title": title, "description": desc, "tags": [TAG], "editable": True, "graphTooltip": 1,
        "schemaVersion": 39, "refresh": refresh, "time": {"from": time_from, "to": "now"},
        "timepicker": {"refresh_intervals": ["10s", "30s", "1m", "5m", "15m"]},
        "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                  "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                                  "name": "Annotations & Alerts", "type": "dashboard"}]},
        "links": [{"title": "Gravitee dashboards", "type": "dashboards", "tags": [TAG], "asDropdown": True,
                   "includeVars": True, "keepTime": True, "icon": "external link"}],
        "templating": {"list": variables}, "panels": panels,
    }


# ============================================================== 1. OVERVIEW
def overview():
    L = Layout()
    P = []
    P.append(row(L, "At a glance"))
    P.append(stat(L, "API calls", [es("A")], w=4, desc="Total API calls handled by the gateway in the selected range "
                  "(all v4 APIs, all plans).", unit="short", color_mode="background",
                  thresholds=[{"color": "#1f60c4", "value": None}]))
    P.append(stat(L, "Availability (non-5xx)", ratio_stat(F + " AND NOT status:[500 TO 599]"), w=4, unit="percentunit",
                  decimals=2, graph=False, desc="Share of calls that did NOT end in a 5xx. Client errors (4xx) are the "
                  "consumer's fault and do not count against availability.",
                  thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.99},
                              {"color": "green", "value": 0.995}]))
    P.append(stat(L, "Server errors (5xx)", ratio_stat(F + " AND status:[500 TO 599]"), w=4, unit="percentunit",
                  decimals=2, graph=False, desc="5xx share. Includes gateway-generated 5xx (e.g. backend unreachable "
                  "→ 502/504) and 5xx returned by the backend.",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.01},
                              {"color": "red", "value": 0.05}]))
    P.append(stat(L, "Client errors (4xx)", ratio_stat(F + " AND status:[400 TO 499]"), w=4, unit="percentunit",
                  decimals=2, graph=False, desc="4xx share: invalid credentials, rate limiting, not found, bad requests.",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.1},
                              {"color": "red", "value": 0.25}]))
    P.append(stat(L, "P95 response time", [es("A", metrics=[pct("gateway-response-time-ms")], ts=False)], w=4, unit="ms",
                  graph=False, desc="95th percentile of the total time seen by the consumer (gateway + backend).",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 300},
                              {"color": "red", "value": 1000}], calc=True, calc_value="lastNotNull"))
    P.append(stat(L, "P95 gateway overhead", [es("A", metrics=[pct("gateway-latency-ms")], ts=False)], w=4, unit="ms",
                  graph=False, desc="95th percentile of the time spent INSIDE Gravitee (policies, security, routing) "
                  "— i.e. total response time minus backend time. This is the cost of the gateway itself.",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 20},
                              {"color": "red", "value": 50}], calc=True, calc_value="lastNotNull"))
    P.append(stat(L, "Active APIs", [es("A", metrics=[card("api-id")], ts=False)], w=4, graph=False,
                  desc="Distinct APIs that received at least one call in the range.", calc=True,
                  calc_value="lastNotNull"))
    P.append(stat(L, "Active applications", [es("A", F + " AND _exists_:application-name", metrics=[card(APP)],
                                                ts=False)], w=4, graph=False, calc=True, calc_value="lastNotNull",
                  desc="Distinct subscribed applications that called an API (keyless traffic excluded)."))
    P.append(stat(L, "Rate-limited calls (429)", [es("A", F + " AND status:429")], w=4, color_mode="background",
                  desc="Calls rejected by rate-limit / quota / spike-arrest policies. A rising value usually means a "
                  "consumer needs a bigger plan — an upsell signal, not an outage.",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]))
    P.append(stat(L, "Rejected credentials (401/403)", [es("A", F + " AND status:(401 OR 403)")], w=4,
                  color_mode="background", desc="Calls refused by plan security (missing / invalid / expired API key, "
                  "JWT, OAuth2 token) or by access-control policies.",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]))
    P.append(stat(L, "Data served", [es("A", metrics=[sum_("response-content-length")])], w=4, unit="decbytes",
                  desc="Response payload bytes sent to consumers."))
    P.append(stat(L, "Gateway nodes up", [prom("A", "count(up{job=\"gravitee\"} == 1)", instant=True)], w=4,
                  graph=False, calc=True, calc_value="lastNotNull", ds=PROM, color_mode="background",
                  desc="Gateway instances successfully scraped by Prometheus.",
                  thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]))

    P.append(row(L, "Traffic"))
    P.append(ts(L, "Calls by status class", status_class_targets(), w=12, stack=True, bars=True,
                overrides=STATUS_OVERRIDES, legend_calcs=("sum",), desc="Every API call, split by HTTP status class."))
    P.append(ts(L, "Calls by API", [es("A", terms="api-name", alias="{{term api-name}}")], w=12, bars=True,
                stack=True, legend_calcs=("sum",), desc="Click a series name to isolate it."))
    P.append(ts(L, "Server error rate by API (5xx %)",
                [es("A", F + " AND status:[500 TO 599]", terms="api-name", hide=True),
                 es("B", terms="api-name", hide=True), math("C", "$A / $B")], w=12, unit="percentunit",
                legend_calcs=("mean", "max"), desc="5xx calls / all calls, per API.",
                overrides=[ov("C", [{"id": "displayName", "value": "${__field.labels[\"api-name\"]}"}], "byFrameRefID")]))
    P.append(ts(L, "Gateway throughput (Prometheus, req/s)",
                [prom("A", f"sum by (code) (rate(http_server_requests_total{{{GW_PORT}}}[$__rate_interval]))",
                      "{{code}}")], w=12, unit="reqps", legend_calcs=("mean", "max"), stack=True,
                desc="Real-time gateway request rate from the Prometheus endpoint (scraped every 5s). Excludes "
                     "internal-API scrapes."))

    P.append(row(L, "Latency attribution — gateway vs backend"))
    P.append(ts(L, "Response time percentiles (consumer view)",
                [es("A", metrics=[pct("gateway-response-time-ms", ("50", "95", "99"))], alias="{{metric}}")], w=8,
                unit="ms", desc="End-to-end time observed by the consumer.",
                overrides=[ov("/p50/", [{"id": "displayName", "value": "P50"}], "byRegexp"),
                           ov("/p95/", [{"id": "displayName", "value": "P95"}], "byRegexp"),
                           ov("/p99/", [{"id": "displayName", "value": "P99"}], "byRegexp")]))
    P.append(ts(L, "P95: total vs backend vs gateway overhead",
                [es("A", metrics=[pct("gateway-response-time-ms")], alias="Total"),
                 es("B", metrics=[pct("endpoint-response-time-ms")], alias="Backend"),
                 es("C", metrics=[pct("gateway-latency-ms")], alias="Gateway overhead")], w=8, unit="ms",
                overrides=[color_ov("Total", "yellow"), color_ov("Backend", C_BE), color_ov("Gateway overhead", C_GW)],
                desc="If 'Total' moves with 'Backend', the slowness is upstream. If it moves with 'Gateway overhead', "
                     "look at policies (transformations, callouts, caching)."))
    P.append(pie(L, "Where the time goes (avg)",
                 [es("A", metrics=[avg("endpoint-response-time-ms")], ts=False, alias="Backend"),
                  es("B", metrics=[avg("gateway-latency-ms")], ts=False, alias="Gateway")], w=8, unit="ms",
                 overrides=[ov("A", [{"id": "displayName", "value": "Backend"},
                                     {"id": "color", "value": {"mode": "fixed", "fixedColor": C_BE}}], "byFrameRefID"),
                            ov("B", [{"id": "displayName", "value": "Gateway overhead"},
                                     {"id": "color", "value": {"mode": "fixed", "fixedColor": C_GW}}], "byFrameRefID")],
                 desc="Average share of response time spent in the backend vs in the gateway."))
    P.append(table(L, "Latency by API (ms)",
                   [es("A", metrics=[count("1"), pct("gateway-response-time-ms", ("50", "95", "99"), "2"),
                                     pct("endpoint-response-time-ms", ("95",), "3"),
                                     pct("gateway-latency-ms", ("95",), "4")], terms="api-name", ts=False, size=50)],
                   w=24, h=8, sort="Calls",
                   transformations=[organize({"api-name": "API", "Count": "Calls",
                                              "p50.0 gateway-response-time-ms": "P50 total",
                                              "p95.0 gateway-response-time-ms": "P95 total",
                                              "p99.0 gateway-response-time-ms": "P99 total",
                                              "p95.0 endpoint-response-time-ms": "P95 backend",
                                              "p95.0 gateway-latency-ms": "P95 gateway overhead"})],
                   overrides=[ov("/P\\d\\d/", [{"id": "unit", "value": "ms"}], "byRegexp"),
                              ov("API", [{"id": "links", "value": API_LINK}]),
                              cell_color("P95 total", [{"color": "green", "value": None}, {"color": "orange", "value": 300},
                                                       {"color": "red", "value": 1000}]),
                              cell_color("P95 gateway overhead", [{"color": "green", "value": None},
                                                                  {"color": "orange", "value": 20},
                                                                  {"color": "red", "value": 50}])],
                   desc="Click an API name to open its drill-down."))

    P.append(row(L, "Leaderboards"))
    P.append(bargauge(L, "Top APIs by calls", [es("A", terms="api-name", ts=False, alias="{{term api-name}}")], w=8,
                      links=API_LINK))
    P.append(bargauge(L, "Top APIs by server errors (5xx)", [es("A", F + " AND status:[500 TO 599]", terms="api-name",
                                                                ts=False)], w=8, links=API_LINK,
                      thresholds=[{"color": "orange", "value": None}, {"color": "red", "value": 50}]))
    P.append(bargauge(L, "Top APIs by P95 response time", [es("A", metrics=[pct("gateway-response-time-ms")],
                                                              terms="api-name", ts=False,
                                                              order_by="1[95.0]")], w=8, unit="ms",
                      thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 300},
                                  {"color": "red", "value": 1000}], links=API_LINK))
    P.append(bargauge(L, "Top applications by calls", [es("A", terms=APP, missing=NO_APP, ts=False)], w=8,
                      mappings=APP_MAP, desc="Subscribed applications. 'Anonymous (keyless)' groups keyless-plan "
                      "traffic. With Enterprise stamping, see the Consumers dashboard for names."))
    P.append(bargauge(L, "Top plans by calls", plan_targets(), w=8, desc=PLAN_DESC))
    P.append(bargauge(L, "Top endpoints (path) by calls", [es("A", terms="uri", ts=False, size=10)], w=8))

    P.append(row(L, "Policy & security outcomes"))
    P.append(ts(L, "Gateway rejections by reason (error key)",
                [es("A", F + " AND _exists_:error-key", terms="error-key", alias="{{term error-key}}", size=15)],
                w=12, bars=True, stack=True, legend_calcs=("sum",),
                desc="Every call the gateway refused or failed itself, by Gravitee error key: "
                     "RATE_LIMIT_TOO_MANY_REQUESTS, QUOTA_TOO_MANY_REQUESTS, GATEWAY_PLAN_UNRESOLVABLE (no/invalid "
                     "credentials), JWT_INVALID_TOKEN, API_KEY_INVALID, GATEWAY_CLIENT_CONNECTION_ERROR (backend "
                     "unreachable), REQUEST_TIMEOUT… Tells you WHY, not just the status code."))
    P.append(pie(L, "Rejections by reason", [es("A", F + " AND _exists_:error-key", terms="error-key", ts=False,
                                               size=15)], w=6, h=9))
    P.append(bargauge(L, "Rejections by policy", [es("A", F + " AND _exists_:error-component-name",
                                                    terms="error-component-name", ts=False)], w=6,
                      thresholds=[{"color": "orange", "value": None}],
                      desc="Which policy or component stopped the call (security, rate-limit, quota, jwt, "
                           "ip-filtering, json-validation…), straight from the request record."))
    P.append(bargauge(L, "Rate-limited (429) by application",
                      [es("A", F + " AND status:429", terms=APP, missing=NO_APP, ts=False)], w=12, h=6, mappings=APP_MAP,
                      thresholds=[{"color": "orange", "value": None}],
                      desc="Who is hitting their plan limits — candidates for a plan upgrade."))
    P.append(table(L, "Credential & access failures by API", [
        es("A", F + " AND status:(401 OR 403)", metrics=[count("1")], terms=["api-name", "status"], ts=False, size=20)],
        w=12, h=8, sort="Count", transformations=[organize({"api-name": "API", "status": "Status"})]))
    P.append(table(L, "Backend failures by API & endpoint", [
        es("A", F + " AND status:[500 TO 599]", metrics=[count("1"), avg("endpoint-response-time-ms", "2")],
           terms=["api-name", "status"], ts=False, size=20)],
        w=12, h=8, sort="Count", transformations=[organize({"api-name": "API", "status": "Status",
                                                             "Average endpoint-response-time-ms": "Avg backend ms",
                                                             "Average": "Avg backend ms"})]))

    P.append(row(L, "Consumers & geography"))
    P.append(geomap(L, "Calls by country", w=12, h=11))
    P.append(table(L, "Top countries", [es("A", metrics=[count("1"), pct("gateway-response-time-ms", ("95",), "2")],
                                           terms="geoip.country_name.keyword", ts=False, size=20)], w=6, h=11, sort="Calls",
                   transformations=[organize({"geoip.country_name.keyword": "Country", "Count": "Calls",
                                              "p95.0 gateway-response-time-ms": "P95 ms"})]))
    P.append(pie(L, "Client types (user agent)", [es("A", terms="user_agent.name", ts=False, size=10)], w=6, h=11))

    return dashboard("gravitee-overview", "Gravitee · API Program Overview",
                     "Executive and operations view of every API managed by Gravitee APIM: traffic, availability, "
                     "latency attribution, policy outcomes, consumers and geography.",
                     P, common_vars())


def geomap(L, title, w=12, h=10, query=F):
    p = base(L, "geomap", title, w, h, [es("A", query, terms="geoip.country_iso_code", ts=False, size=250)],
             desc="Calls by consumer country (Elasticsearch geo-IP on the client address; honours X-Forwarded-For).",
             color={"mode": "continuous-BlPu"})
    p["options"] = {
        "view": {"id": "zero", "lat": 25, "lon": 10, "zoom": 1.4, "allLayers": True},
        "controls": {"showZoom": True, "mouseWheelZoom": False, "showAttribution": False},
        # Public Esri canvas tiles (Grafana's default CARTO basemap now requires an API key)
        "basemap": {"type": "xyz", "name": "Basemap", "config": {
            "url": "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
            "attribution": "Tiles © Esri"}},
        "layers": [{"type": "markers", "name": "Calls", "tooltip": True,
                    "location": {"mode": "lookup", "lookup": "geoip.country_iso_code",
                                 "gazetteer": "public/gazetteer/countries.json"},
                    "config": {"showLegend": True,
                               "style": {"size": {"field": "Count", "min": 6, "max": 40, "fixed": 5},
                                         "color": {"fixed": "#B877D9"}, "opacity": 0.55,
                                         "text": {"mode": "field", "field": "Count", "fixed": ""}, "symbol": {"mode": "fixed", "fixed": "img/icons/marker/circle.svg"},
                                         "textConfig": {"fontSize": 10, "offsetY": 0, "textAlign": "center",
                                                        "textBaseline": "middle"}}}}],
        "tooltip": {"mode": "details"}}
    return p


# ============================================================== 2. API DRILL-DOWN
def api_detail():
    L = Layout()
    P = []
    P.append(row(L, "API health"))
    P.append(stat(L, "Calls", [es("A")], w=4, color_mode="background", thresholds=[{"color": "#1f60c4", "value": None}]))
    P.append(stat(L, "Availability (non-5xx)", ratio_stat(F + " AND NOT status:[500 TO 599]"), w=4, unit="percentunit",
                  decimals=2, graph=False, thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.99},
                                                       {"color": "green", "value": 0.995}]))
    P.append(stat(L, "Error rate (4xx + 5xx)", ratio_stat(F + " AND status:[400 TO 599]"), w=4, unit="percentunit",
                  decimals=2, graph=False, thresholds=[{"color": "green", "value": None},
                                                       {"color": "orange", "value": 0.05},
                                                       {"color": "red", "value": 0.15}]))
    P.append(stat(L, "P95 total", [es("A", metrics=[pct("gateway-response-time-ms")], ts=False)], w=4, unit="ms",
                  graph=False, calc=True, calc_value="lastNotNull",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 300},
                              {"color": "red", "value": 1000}]))
    P.append(stat(L, "P95 backend", [es("A", metrics=[pct("endpoint-response-time-ms")], ts=False)], w=4, unit="ms",
                  graph=False, calc=True, calc_value="lastNotNull"))
    P.append(stat(L, "P95 gateway overhead", [es("A", metrics=[pct("gateway-latency-ms")], ts=False)], w=4,
                  unit="ms", graph=False, calc=True, calc_value="lastNotNull",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 20},
                              {"color": "red", "value": 50}]))

    P.append(row(L, "Traffic & errors"))
    P.append(ts(L, "Calls by status class", status_class_targets(), w=12, stack=True, bars=True,
                overrides=STATUS_OVERRIDES, legend_calcs=("sum",)))
    P.append(ts(L, "Calls by status code", [es("A", terms="status", alias="{{term status}}", size=20)], w=12,
                stack=True, legend_calcs=("sum",),
                overrides=[color_ov("/^2/", C_OK, "byRegexp"), color_ov("/^4/", C_4XX, "byRegexp"),
                           color_ov("/^5/", C_5XX, "byRegexp")]))
    P.append(ts(L, "Calls by HTTP method", [es("A", terms="http-method", alias="{{term http-method}}")], w=8,
                stack=True, legend_calcs=("sum",),
                overrides=[ov("3", [{"id": "displayName", "value": "GET"}]), ov("7", [{"id": "displayName", "value": "POST"}]),
                           ov("8", [{"id": "displayName", "value": "PUT"}]), ov("2", [{"id": "displayName", "value": "DELETE"}]),
                           ov("6", [{"id": "displayName", "value": "PATCH"}])]))
    P.append(ts(L, "Rejections by reason (error key)",
                [es("A", F + " AND _exists_:error-key", terms="error-key", alias="{{term error-key}}")], w=8, bars=True,
                stack=True, legend_calcs=("sum",)))
    P.append(ts(L, "Payload size (avg)", [es("A", metrics=[avg("request-content-length")], alias="Request"),
                                          es("B", metrics=[avg("response-content-length")], alias="Response")], w=8,
                unit="decbytes"))

    P.append(row(L, "Latency deep-dive"))
    P.append(ts(L, "Latency breakdown (P95)",
                [es("A", metrics=[pct("gateway-response-time-ms")], alias="Total"),
                 es("B", metrics=[pct("endpoint-response-time-ms")], alias="Backend"),
                 es("C", metrics=[pct("endpoint-response-ttfb-ms")], alias="Backend time-to-first-byte"),
                 es("D", metrics=[pct("endpoint-connect-time-ms")], alias="Backend connect"),
                 es("E", metrics=[pct("gateway-latency-ms")], alias="Gateway overhead")], w=12, unit="ms",
                overrides=[color_ov("Total", "yellow"), color_ov("Backend", C_BE), color_ov("Gateway overhead", C_GW)],
                desc="Connect time spikes → network/DNS/TLS to the backend. TTFB ≈ backend processing. "
                     "Gateway overhead → policies."))
    P.append(base(L, "heatmap", "Response time distribution", 12, 8,
                  [es("A", metrics=[count()], terms=None, ts=False) | {
                      "bucketAggs": [{"id": "3", "type": "histogram", "field": "gateway-response-time-ms",
                                      "settings": {"interval": "50", "min_doc_count": "1"}}, _dh()]}],
                  desc="Each cell = number of calls with that response time (50 ms buckets). Bands reveal bimodal "
                       "behaviour (cache hits vs misses, fast vs slow backends).",
                  options={"calculate": False, "yAxis": {"unit": "ms", "axisPlacement": "left"},
                           "color": {"mode": "scheme", "scheme": "Oranges", "steps": 64, "exponent": 0.5},
                           "cellGap": 1, "tooltip": {"mode": "single", "yHistogram": True},
                           "rowsFrame": {"layout": "auto"}}))
    P.append(table(L, "Endpoints: traffic, errors & latency", [
        es("A", metrics=[count("1"), pct("gateway-response-time-ms", ("50", "95"), "2"),
                         avg("endpoint-response-time-ms", "3"), max_("gateway-response-time-ms", "4")],
           terms="path-info.keyword", ts=False, size=30),
        es("B", F + " AND status:[500 TO 599]", metrics=[count("1")], terms="path-info.keyword", ts=False, size=30)],
        w=24, h=9, sort="Calls",
        transformations=[join("path-info.keyword"),
                         organize({"path-info.keyword": "Path", "Count": "Calls", "Count 1": "Calls",
                                   "p50.0 gateway-response-time-ms": "P50 ms", "p95.0 gateway-response-time-ms": "P95 ms",
                                   "Average endpoint-response-time-ms": "Avg backend ms", "Average": "Avg backend ms",
                                   "Max gateway-response-time-ms": "Max ms", "Max": "Max ms", "Count 2": "5xx"})],
        overrides=[cell_color("P95 ms", [{"color": "green", "value": None}, {"color": "orange", "value": 300},
                                         {"color": "red", "value": 1000}]),
                   cell_color("5xx", [{"color": "transparent", "value": None}, {"color": "red", "value": 1}])],
        desc="Per-path view (path after the API context path), recorded by Gravitee on every call — no per-path "
             "metric to configure."))

    P.append(row(L, "Consumers of this API"))
    P.append(table(L, "Applications", [
        es("A", metrics=[count("1"), pct("gateway-response-time-ms", ("95",), "2")], terms=APP, missing=NO_APP, ts=False,
           size=50),
        es("B", F + " AND status:[400 TO 599]", metrics=[count("1")], terms=APP, missing=NO_APP, ts=False, size=50)],
        w=12, h=9, sort="Calls", mappings=APP_MAP,
        transformations=[join(APP),
                         organize({APP: "Application", "Count": "Calls", "Count 1": "Calls",
                                   "p95.0 gateway-response-time-ms": "P95 ms", "Count 2": "Errors"})]))
    P.append(ts(L, "Calls by application", [es("A", terms=APP, missing=NO_APP, alias="{{term application-name.keyword}}")], w=12,
                stack=True, legend_calcs=("sum",), mappings=APP_MAP))
    P.append(pie(L, "Calls by plan", plan_targets(), w=8, desc=PLAN_DESC))
    P.append(pie(L, "Calls by country", [es("A", terms="geoip.country_name.keyword", ts=False)], w=8))
    P.append(pie(L, "Client types", [es("A", terms="user_agent.name", ts=False)], w=8))

    P.append(row(L, "Investigate"))
    P.append(table(L, "Latest failed calls (4xx/5xx)", [
        es("A", F + " AND status:[400 TO 599]", metrics=[raw(200)], ts=False)], w=24, h=11,
        transformations=[organize(
            {"@timestamp": "Time", "status": "Status", "http-method": "Method", "uri": "URI",
             "error-key": "Error key", "application-name": "Application", "plan-id": "Plan",
             "gateway-response-time-ms": "Total ms", "endpoint-response-time-ms": "Backend ms",
             "remote-address": "Client IP", "geoip.country_name.keyword": "Country", "request-id": "Request id"},
            order=["@timestamp", "request-id", "status", "http-method", "uri", "error-key", "application-name",
                   "plan-id", "gateway-response-time-ms", "endpoint-response-time-ms", "remote-address",
                   "geoip.country_name.keyword"]),
            {"id": "filterFieldsByName", "options": {"include": {"names": [
                "@timestamp", "status", "http-method", "uri", "error-key", "application-name", "plan-id",
                "gateway-response-time-ms", "endpoint-response-time-ms", "remote-address", "geoip.country_name.keyword",
                "request-id"]}}}],
        overrides=[cell_color("status", [{"color": "orange", "value": None}, {"color": "red", "value": 500}]),
                   ov("http-method", [{"id": "mappings", "value": METHOD_MAP}]), CALL_LOG_LINK],
        desc="Raw request records. Click a request id to open the call's headers and payloads in Call Logs "
             "(available when API logging is enabled and the call was logged)."))
    P.append(table(L, "Slowest calls", [
        es("A", F + " AND gateway-response-time-ms:>=500", metrics=[raw(100)], ts=False)], w=24, h=9,
        transformations=[{"id": "filterFieldsByName", "options": {"include": {"names": [
            "@timestamp", "status", "uri", "application-name", "gateway-response-time-ms", "endpoint-response-time-ms",
            "gateway-latency-ms", "endpoint-connect-time-ms", "request-id"]}}},
            organize({"@timestamp": "Time", "status": "Status", "uri": "URI", "application-name": "Application",
                      "gateway-response-time-ms": "Total ms", "endpoint-response-time-ms": "Backend ms",
                      "gateway-latency-ms": "Gateway ms", "endpoint-connect-time-ms": "Connect ms",
                      "request-id": "Request id"},
                     order=["@timestamp", "request-id", "status", "uri", "application-name", "gateway-response-time-ms",
                            "endpoint-response-time-ms", "gateway-latency-ms", "endpoint-connect-time-ms"])],
        overrides=[CALL_LOG_LINK],
        desc="Calls slower than 500 ms, with the gateway/backend/connect split for each one. Click a request id "
             "to open its call log."))

    P.append(row(L, "Call logs of this API"))
    drill_cols = {k: v for k, v in LOG_COLS.items() if k != "api-name"}
    P.append(log_table(L, "Logged failed calls", "api-name:$api AND entrypoint-response.status:[400 TO 599]", h=9,
                       cols=drill_cols,
                       desc="Failed calls of this API that have a call log (headers and payloads). Click a row's time, "
                            "URI or request id to open it. Empty when logging isn't enabled on the API."))
    P.append(log_table(L, "Latest logged calls", "api-name:$api", h=9, cols=drill_cols,
                       desc="All calls of this API that have a call log. Click a row's time, URI or request id to "
                            "open its headers and payloads."))

    v = common_vars()
    v[2] = es_terms_var("api", "API", "api-name", multi=False)
    v[2]["includeAll"] = False
    v[2]["current"] = {}
    v.insert(2, ds_var("ds_logs", "Call logs (Elasticsearch)", "elasticsearch", "gravitee-logs"))
    d = dashboard("gravitee-api-detail", "Gravitee · API Drill-down",
                     "Everything about one API: health, status codes, methods, per-path latency, consumers, and the "
                     "individual failed or slow calls, down to their call logs.", P, v)
    d["links"].append({"title": "Call logs of this API", "type": "link", "icon": "doc", "keepTime": True,
                       "url": "/d/gravitee-logs?${api:queryparam}&${ds_logs:queryparam}&${ds_analytics:queryparam}",
                       "tooltip": "Headers and payloads of this API's logged calls"})
    return d


# ============================================================== 3. CONSUMERS
def consumers():
    L = Layout()
    P = []
    P.append(row(L, "Applications"))
    P.append(stat(L, "Active applications", [es("A", F + " AND _exists_:application-name", metrics=[card(APP)],
                                                ts=False)], w=6, graph=False, calc=True, calc_value="lastNotNull"))
    P.append(stat(L, "Active subscriptions", [es("A", F + " AND _exists_:application-name",
                                                 metrics=[card("subscription-id")], ts=False)], w=6, graph=False,
                  calc=True, calc_value="lastNotNull"))
    P.append(stat(L, "Keyless share", ratio_stat(F + " AND NOT _exists_:application-name"), w=6, unit="percentunit", decimals=1,
                  graph=False, desc="Share of calls on keyless (anonymous) plans."))
    P.append(stat(L, "Distinct client IPs", [es("A", metrics=[card("remote-address")], ts=False)], w=6, graph=False,
                  calc=True, calc_value="lastNotNull"))
    P.append(table(L, "Application scorecard", [
        es("A", metrics=[count("1"), pct("gateway-response-time-ms", ("95",), "2"), card("api-id", "3"),
                         sum_("response-content-length", "4")], terms=APP, missing=NO_APP, ts=False, size=100),
        es("B", F + " AND status:[400 TO 499]", metrics=[count("1")], terms=APP, missing=NO_APP, ts=False, size=100),
        es("C", F + " AND status:[500 TO 599]", metrics=[count("1")], terms=APP, missing=NO_APP, ts=False, size=100),
        es("D", F + " AND status:429", metrics=[count("1")], terms=APP, missing=NO_APP, ts=False, size=100)],
        w=24, h=9, sort="Calls", mappings=APP_MAP,
        transformations=[join(APP),
                         organize({APP: "Application", "Count": "Calls", "Count 1": "Calls",
                                   "p95.0 gateway-response-time-ms": "P95 ms", "Unique Count api-id": "APIs used",
                                   "Unique Count": "APIs used", "Sum": "Bytes served",
                                   "Sum response-content-length": "Bytes served", "Count 2": "4xx", "Count 3": "5xx",
                                   "Count 4": "429 (rate-limited)"})],
        overrides=[unit_ov("Bytes served", "decbytes"),
                   cell_color("429 (rate-limited)", [{"color": "transparent", "value": None},
                                                     {"color": "orange", "value": 1}]),
                   cell_color("5xx", [{"color": "transparent", "value": None}, {"color": "red", "value": 1}])],
        desc="One line per application: volume, quality of service received, breadth of API usage, and "
             "rate-limit pressure."))
    P.append(ts(L, "Calls by application", [es("A", terms=APP, missing=NO_APP, alias="{{term application-name.keyword}}")], w=12,
                stack=True, legend_calcs=("sum",), mappings=APP_MAP))
    P.append(ts(L, "Rate-limited calls by application", [
        es("A", F + " AND status:429", terms=APP, missing=NO_APP, alias="{{term application-name.keyword}}")], w=12, bars=True,
        stack=True, legend_calcs=("sum",), mappings=APP_MAP))

    P.append(row(L, "Plans & subscriptions"))
    P.append(table(L, "Plan usage", [
        es("A", metrics=[count("1"), card(APP, "2"), pct("gateway-response-time-ms", ("95",), "3")],
           terms=["api-name", "plan-id"], ts=False, size=50),
        es("B", F + " AND status:429", metrics=[count("1")], terms=["api-name", "plan-id"], ts=False, size=50)],
        w=14, h=9, sort="Calls",
        transformations=[join("plan-id"),
                         organize({"api-name 1": "API", "api-name": "API", "plan-id": "Plan id", "Count": "Calls",
                                   "Count 1": "Calls", "Unique Count": "Applications",
                                   "Unique Count application-name.keyword": "Applications",
                                   "p95.0 gateway-response-time-ms": "P95 ms", "Count 2": "429"},
                                  exclude=["api-name 2"], order=["api-name 1", "plan-id"])]))
    P.append(bargauge(L, "Top subscriptions by calls", [es("A", F + " AND _exists_:application-name",
                                                           terms="subscription-id", ts=False)], w=10, h=9))

    P.append(row(L, "Business dimensions (custom metrics)"))
    P.append(text(L, "How to add your own dimensions", CUSTOM_HELP, w=8, h=10))
    P.append(ts(L, "Calls by tenant (custom.tenant)", [es("A", F + " AND _exists_:custom.tenant",
                                                          terms="custom.tenant", alias="{{term custom.tenant}}")],
                w=8, h=10, stack=True, legend_calcs=("sum",),
                desc="Populated by the Assign Metrics policy (custom.tenant ← X-Tenant-ID header)."))
    P.append(table(L, "Tenant scorecard", [
        es("A", F + " AND _exists_:custom.tenant", metrics=[count("1"), pct("gateway-response-time-ms", ("95",), "2")],
           terms="custom.tenant", ts=False, size=50),
        es("B", F + " AND _exists_:custom.tenant AND status:[500 TO 599]", metrics=[count("1")], terms="custom.tenant",
           ts=False, size=50)], w=8, h=10, sort="Calls",
        transformations=[join("custom.tenant"),
                         organize({"custom.tenant": "Tenant", "Count": "Calls", "Count 1": "Calls",
                                   "p95.0 gateway-response-time-ms": "P95 ms", "Count 2": "5xx"})]))
    P.append(bargauge(L, "Calls by plan name (custom.plan)", [
        es("A", F + " AND _exists_:custom.plan", terms="custom.plan", ts=False)], w=12, h=7,
        desc="Analytics store plan ids; stamp a readable plan name with Assign Metrics on each plan's flow."))
    P.append(pie(L, "Calls by channel (custom.channel)", [
        es("A", F + " AND _exists_:custom.channel", terms="custom.channel", ts=False)], w=12, h=7))
    for p in P[-5:]:
        if p["type"] == "table":
            p["fieldConfig"]["overrides"].append(ov("5xx", [{"id": "noValue", "value": "0"}]))
        elif p["type"] != "text":
            p["fieldConfig"]["defaults"]["noValue"] = "Add the Assign Metrics policy to populate (see left)"

    P.append(row(L, "Where consumers are"))
    P.append(geomap(L, "Calls by country", w=12, h=11))
    P.append(pie(L, "Client OS / device", [es("A", terms="user_agent.os_name", ts=False, size=10)], w=6, h=11))
    P.append(table(L, "User agents", [es("A", metrics=[count("1")], terms="user-agent", ts=False, size=30)], w=6, h=11,
                   sort="Count", transformations=[organize({"user-agent": "User agent"})]))
    return dashboard("gravitee-consumers", "Gravitee · Consumers, Plans & Tenants",
                     "Who consumes your APIs, how much, with what quality of service, from where — plus business "
                     "dimensions (tenant, channel, …) stamped by the Assign Metrics policy.", P, common_vars())


CUSTOM_HELP = """
Gravitee records **API, application, plan id, subscription, geo-IP, user agent and the rejecting policy on every call — no configuration**.

Add **any business dimension** (tenant, customer, region, product…) with the **Assign Metrics** policy
on an API or plan flow. Each metric lands as `custom.<name>` on the same record and appears here:

```json
{ "policy": "policy-assign-metrics",
  "configuration": { "scope": "REQUEST", "metrics": [
    { "name": "tenant",
      "value": "{#request.headers['X-Tenant-ID'] != null ? #request.headers['X-Tenant-ID'][0] : 'unknown'}" },
    { "name": "plan", "value": "Gold" } ] } }
```

Always give the expression a fallback: a null value drops the metric.
_Assign Metrics is an Enterprise Edition policy._
"""


# ============================================================== 4. GATEWAY RUNTIME
def runtime():
    L = Layout()
    P = []
    inst = 'instance=~"$instance"'
    P.append(row(L, "Fleet at a glance"))
    P.append(stat(L, "Gateway nodes up", [prom("A", 'count(up{job="gravitee"} == 1)', instant=True)], w=4, graph=False,
                  calc=True, calc_value="lastNotNull", color_mode="background",
                  thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]))
    P.append(stat(L, "Request rate", [prom("A", f"sum(rate(http_server_requests_total{{{GW_PORT},{inst}}}[$__rate_interval]))")],
                  w=4, unit="reqps", calc=True, calc_value="lastNotNull"))
    P.append(stat(L, "Active connections", [prom("A", f"sum(http_server_active_connections{{{GW_PORT},{inst}}})")], w=4,
                  calc=True, calc_value="lastNotNull"))
    P.append(stat(L, "Heap used (max node)", [prom("A", f'max(sum by (instance) (jvm_memory_used_bytes{{area="heap",{inst}}}) / '
                                                       f'sum by (instance) (jvm_memory_max_bytes{{area="heap",{inst}}} > 0))')],
                  w=4, unit="percentunit", calc=True, calc_value="lastNotNull",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.75},
                              {"color": "red", "value": 0.9}]))
    P.append(stat(L, "Process CPU (max node)", [prom("A", f"max(process_cpu_usage{{{inst}}})")], w=4,
                  unit="percentunit", calc=True, calc_value="lastNotNull",
                  thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.7},
                              {"color": "red", "value": 0.9}]))
    P.append(stat(L, "JVM uptime", [es("A", "*", metrics=[{"id": "1", "type": "max",
                                                                        "field": "jvm.uptime_in_millis"}],
                                                  ts=False, ds=MON)], w=4, unit="ms",
                  graph=False, calc=True, calc_value="lastNotNull", ds=MON,
                  desc="Time since the youngest node started — a low value means a recent restart."))
    P.append(table(L, "Gateway nodes", [
        prom("A", 'max by (instance) (up{job="gravitee"})', instant=True),
        prom("B", f'sum by (instance) (rate(http_server_requests_total{{{GW_PORT}}}[5m]))', instant=True),
        prom("C", 'sum by (instance) (jvm_memory_used_bytes{area="heap"}) / sum by (instance) (jvm_memory_max_bytes{area="heap"} > 0)',
             instant=True),
        prom("D", "max by (instance) (process_cpu_usage)", instant=True),
        prom("E", "max by (instance) (jvm_threads_live_threads)", instant=True),
        prom("F", "max by (instance) (process_files_open_files)", instant=True)], w=24, h=6,
        transformations=[{"id": "merge", "options": {}},
                         {"id": "filterFieldsByName", "options": {"include": {"pattern": "instance|Value.*"}}},
                         organize({"instance": "Node", "Value #A": "Up", "Value #B": "Req/s", "Value #C": "Heap %",
                                   "Value #D": "CPU", "Value #E": "Threads", "Value #F": "Open files"})],
        overrides=[unit_ov("Heap %", "percentunit"), unit_ov("CPU", "percentunit"), unit_ov("Req/s", "reqps"),
                   cell_color("Up", [{"color": "red", "value": None}, {"color": "green", "value": 1}]),
                   ov("Up", [{"id": "mappings", "value": [{"type": "value", "options": {
                       "1": {"text": "UP"}, "0": {"text": "DOWN"}}}]}]),
                   cell_color("Heap %", [{"color": "green", "value": None}, {"color": "orange", "value": 0.75},
                                         {"color": "red", "value": 0.9}], "color-text")],
        ds=PROM))

    P.append(row(L, "Inbound traffic (consumers → gateway)"))
    P.append(ts(L, "Requests by node", [prom("A", f"sum by (instance) (rate(http_server_requests_total{{{GW_PORT},{inst}}}[$__rate_interval]))",
                                             "{{instance}}")], w=8, unit="reqps"))
    P.append(ts(L, "Requests by status code", [prom("A", f"sum by (code) (rate(http_server_requests_total{{{GW_PORT},{inst}}}[$__rate_interval]))",
                                                    "{{code}}")], w=8, unit="reqps", stack=True,
                overrides=[color_ov("/^2/", C_OK, "byRegexp"), color_ov("/^4/", C_4XX, "byRegexp"),
                           color_ov("/^5/", C_5XX, "byRegexp")]))
    P.append(ts(L, "Average response time (gateway-wide)", [
        prom("A", f"sum(rate(http_server_response_time_seconds_sum{{{GW_PORT},{inst}}}[$__rate_interval])) / "
                  f"sum(rate(http_server_response_time_seconds_count{{{GW_PORT},{inst}}}[$__rate_interval]))", "avg"),
        prom("B", f"max(http_server_response_time_seconds_max{{{GW_PORT},{inst}}})", "max")], w=8, unit="s"))
    # http_server_active_requests is left out on purpose: on 4.13.5 it never decrements for some rejected
    # calls (observed climbing to ~500 with 2 open connections), so it reads as a phantom load.
    P.append(ts(L, "Active client connections", [
        prom("A", f"sum by (instance) (http_server_active_connections{{{GW_PORT},{inst}}})", "{{instance}}")],
        w=12, desc="Open HTTP connections from consumers to the gateway, per node."))
    P.append(ts(L, "Network throughput", [
        prom("A", f"sum(rate(http_server_bytes_read_total{{{GW_PORT},{inst}}}[$__rate_interval]))", "in (from consumers)"),
        prom("B", f"sum(rate(http_server_bytes_written_total{{{GW_PORT},{inst}}}[$__rate_interval]))", "out (to consumers)")],
        w=12, unit="Bps"))

    P.append(row(L, "Outbound traffic (gateway → backends)"))
    P.append(ts(L, "Backend requests by status", [
        prom("A", f"sum by (code) (rate(http_client_requests_total{{{inst}}}[$__rate_interval]))", "{{code}}")], w=8,
        unit="reqps", stack=True, desc="Calls the gateway made to upstream services (Vert.x HTTP client).",
        overrides=[color_ov("/^2/", C_OK, "byRegexp"), color_ov("/^4/", C_4XX, "byRegexp"),
                   color_ov("/^5/", C_5XX, "byRegexp")]))
    P.append(ts(L, "Backend response time", [
        prom("A", f"sum by (instance) (rate(http_client_response_time_seconds_sum{{{inst}}}[$__rate_interval])) / "
                  f"sum by (instance) (rate(http_client_response_time_seconds_count{{{inst}}}[$__rate_interval]))",
             "avg {{instance}}"),
        prom("B", f"max by (instance) (http_client_response_time_seconds_max{{{inst}}})", "max {{instance}}")], w=8,
        unit="s", desc="Upstream response time measured by the gateway's HTTP client (all backends)."))
    P.append(ts(L, "Backend connection pool", [
        prom("A", f"sum by (instance) (http_client_active_connections{{{inst}}})", "connections {{instance}}"),
        prom("B", f"sum by (instance) (http_client_active_requests{{{inst}}})", "in-flight {{instance}}")], w=8,
        desc="Open connections and in-flight requests per backend. A pool pinned at its maximum while latency "
             "rises = backend saturation."))

    P.append(row(L, "JVM"))
    P.append(ts(L, "Heap used vs max", [
        prom("A", f'sum by (instance) (jvm_memory_used_bytes{{area="heap",{inst}}})', "used {{instance}}"),
        prom("B", f'sum by (instance) (jvm_memory_committed_bytes{{area="heap",{inst}}})', "committed {{instance}}"),
        prom("C", f'sum by (instance) (jvm_memory_max_bytes{{area="heap",{inst}}} > 0)', "max {{instance}}")], w=8,
        unit="bytes", overrides=[ov("/max/", [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                              {"id": "custom.fillOpacity", "value": 0}], "byRegexp")]))
    P.append(ts(L, "Heap by pool", [prom("A", f'sum by (id) (jvm_memory_used_bytes{{area="heap",{inst}}})', "{{id}}")],
                w=8, unit="bytes", stack=True))
    P.append(ts(L, "Non-heap (metaspace, code cache…)", [
        prom("A", f'sum by (id) (jvm_memory_used_bytes{{area="nonheap",{inst}}})', "{{id}}")], w=8, unit="bytes",
        stack=True))
    P.append(ts(L, "GC pause time (per second)", [
        prom("A", f"sum by (action, cause) (rate(jvm_gc_pause_seconds_sum{{{inst}}}[$__rate_interval]))",
             "{{action}} — {{cause}}")], w=8, unit="s",
        desc="Seconds spent paused in GC per second of wall time. Sustained > 0.05 hurts tail latency."))
    P.append(ts(L, "GC pauses (count/s) & max pause", [
        prom("A", f"sum(rate(jvm_gc_pause_seconds_count{{{inst}}}[$__rate_interval]))", "pauses/s"),
        prom("B", f"max(jvm_gc_pause_seconds_max{{{inst}}})", "max pause (s)")], w=8,
        overrides=[unit_ov("max pause (s)", "s")]))
    P.append(ts(L, "Allocation & promotion rate", [
        prom("A", f"sum(rate(jvm_gc_memory_allocated_bytes_total{{{inst}}}[$__rate_interval]))", "allocated"),
        prom("B", f"sum(rate(jvm_gc_memory_promoted_bytes_total{{{inst}}}[$__rate_interval]))", "promoted to old gen")],
        w=8, unit="Bps"))
    P.append(ts(L, "Threads", [prom("A", f"sum by (state) (jvm_threads_states_threads{{{inst}}})", "{{state}}")], w=8,
                stack=True))
    P.append(ts(L, "Classes loaded", [prom("A", f"sum(jvm_classes_loaded_classes{{{inst}}})", "loaded")], w=8))
    P.append(ts(L, "Direct / mapped buffers", [
        prom("A", f"sum by (id) (jvm_buffer_memory_used_bytes{{{inst}}})", "{{id}}")], w=8, unit="bytes",
        desc="Off-heap buffers used by Netty / Vert.x I/O."))

    P.append(row(L, "Host & process"))
    P.append(ts(L, "CPU usage", [prom("A", f"process_cpu_usage{{{inst}}}", "process {{instance}}"),
                                 prom("B", f"system_cpu_usage{{{inst}}}", "system {{instance}}")], w=8,
                unit="percentunit", maxv=1))
    P.append(ts(L, "Load average (1m) vs CPU count", [
        prom("A", f"system_load_average_1m{{{inst}}}", "load {{instance}}"),
        prom("B", f"system_cpu_count{{{inst}}}", "cpus {{instance}}")], w=8))
    P.append(ts(L, "Open file descriptors", [
        prom("A", f"process_files_open_files{{{inst}}}", "open {{instance}}")], w=8,
        desc="Sockets count as files. Compare with process_files_max_files."))

    P.append(row(L, "Node monitoring (Elasticsearch — no Prometheus required)", collapsed=False))
    P.append(ts(L, "Heap used % per node", [
        es("A", "*", metrics=[max_("jvm.mem.heap_used_percent")], terms="hostname", alias="{{term hostname}}",
           ds=MON)], w=8, unit="percent", ds=MON,
        desc="From the gateway's built-in monitoring reporter (gravitee-monitor-* index)."))
    P.append(ts(L, "OS memory used % per node", [
        es("A", "*", metrics=[max_("os.mem.used_percent")], terms="hostname", alias="{{term hostname}}", ds=MON)],
        w=8, unit="percent", ds=MON))
    P.append(ts(L, "Process CPU % per node", [
        es("A", "*", metrics=[max_("process.cpu.percent")], terms="hostname", alias="{{term hostname}}", ds=MON)],
        w=8, unit="percent", ds=MON))

    v = common_vars(api=False, adhoc=False, monitor=True)
    v.append({"name": "instance", "label": "Node", "type": "query", "datasource": PROM,
              "query": {"query": 'label_values(up{job="gravitee"}, instance)', "refId": "inst"},
              "definition": 'label_values(up{job="gravitee"}, instance)', "refresh": 2, "includeAll": True,
              "multi": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"}, "sort": 1})
    return dashboard("gravitee-runtime", "Gravitee · Gateway Fleet & Runtime",
                     "Gateway nodes, inbound and outbound (backend) HTTP traffic, connection pools, JVM memory and "
                     "GC, threads, CPU and file descriptors.", P, v, time_from="now-30m", refresh="10s")


# ============================================================== 5. SLO
def slo():
    L = Layout()
    P = []
    P.append(row(L, "Service level objectives"))
    P.append(text(L, "How SLOs are computed", SLO_HELP, w=8, h=8))
    P.append(stat(L, "Availability SLI (30d)", [prom("A", "gravitee:sli_availability:ratio_rate30d", instant=True)],
                  w=4, h=8, unit="percentunit", decimals=3, graph=False, calc=True, calc_value="lastNotNull",
                  thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 0.995}],
                  desc="Gateway-wide share of non-5xx calls over 30 days (Prometheus recording rule)."))
    P.append(stat(L, "Error budget remaining", [prom("A", "gravitee:error_budget_remaining:ratio30d", instant=True)],
                  w=4, h=8, unit="percentunit", decimals=1, graph=False, calc=True, calc_value="lastNotNull",
                  thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.25},
                              {"color": "green", "value": 0.5}],
                  desc="1 − (errors consumed / errors allowed by a 99.5% objective over 30 days)."))
    P.append(stat(L, "Latency SLI: calls ≤ 500 ms", ratio_stat(F + " AND gateway-response-time-ms:<=500"), w=4, h=8,
                  unit="percentunit", decimals=2, graph=False,
                  thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.95},
                              {"color": "green", "value": 0.99}],
                  desc="Share of calls answered within 500 ms over the selected range (exact, from analytics)."))
    P.append(stat(L, "Firing SLO alerts", [prom("A", 'count(ALERTS{alertstate="firing", alertname=~"Gravitee.*"}) or vector(0)',
                                                instant=True)], w=4, h=8, graph=False, calc=True, calc_value="lastNotNull",
                  color_mode="background", thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
                  desc="Alerts currently firing from the Prometheus rules shipped with this pack "
                       "(prometheus/rules/gravitee-alerts.rules.yml)."))
    P.append(ts(L, "Error budget burn rate", [
        prom("A", "gravitee:error_burn_rate:5m", "5m"), prom("B", "gravitee:error_burn_rate:1h", "1h"),
        prom("C", "gravitee:error_burn_rate:6h", "6h")], w=12, h=8,
        overrides=[ov("5m", [{"id": "custom.lineWidth", "value": 1}])],
        desc="1 = consuming the budget exactly at the allowed pace. The page alert fires when both 5m and 1h "
             "burn > 14.4 (2% of the monthly budget in 1 hour). Ticket alert: 6h > 6."))
    P.append(ts(L, "Availability SLI (rolling windows)", [
        prom("A", "gravitee:sli_availability:ratio_rate5m", "5m"),
        prom("B", "gravitee:sli_availability:ratio_rate1h", "1h"),
        prom("C", "vector(0.995)", "objective 99.5%")], w=12, h=8, unit="percentunit",
        overrides=[ov("objective 99.5%", [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                          {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                                          {"id": "custom.fillOpacity", "value": 0}])]))

    P.append(row(L, "Per-API SLO compliance (from analytics)"))
    P.append(table(L, "Per-API availability & latency SLIs", [
        es("A", metrics=[count("1"), pct("gateway-response-time-ms", ("95", "99"), "2")], terms="api-name", ts=False,
           size=100),
        es("B", F + " AND status:[500 TO 599]", metrics=[count("1")], terms="api-name", ts=False, size=100),
        es("C", F + " AND gateway-response-time-ms:>500", metrics=[count("1")], terms="api-name", ts=False, size=100)],
        w=24, h=10, sort="Calls",
        transformations=[
            join("api-name"),
            organize({"api-name": "API", "Count": "Calls", "Count 1": "Calls",
                      "p95.0 gateway-response-time-ms": "P95 ms", "p99.0 gateway-response-time-ms": "P99 ms",
                      "Count 2": "5xx", "Count 3": "Slow (>500ms)"}),
            {"id": "calculateField", "options": {"mode": "binary", "alias": "Errors %",
                                                 "binary": {"left": "5xx", "operator": "/", "right": "Calls"},
                                                 "replaceFields": False}},
            {"id": "calculateField", "options": {"mode": "binary", "alias": "Availability",
                                                 "binary": {"left": "1", "operator": "-", "right": "Errors %"}}},
            {"id": "calculateField", "options": {"mode": "binary", "alias": "Slow %",
                                                 "binary": {"left": "Slow (>500ms)", "operator": "/", "right": "Calls"}}},
            {"id": "calculateField", "options": {"mode": "binary", "alias": "Error budget used",
                                                 "binary": {"left": "Errors %", "operator": "/", "right": "0.005"}}},
            organize(exclude=["Errors %"], order=["API", "Calls", "Availability", "Error budget used", "5xx",
                                                  "P95 ms", "P99 ms", "Slow %", "Slow (>500ms)"])],
        overrides=[unit_ov("Availability", "percentunit"), unit_ov("Slow %", "percentunit"),
                   unit_ov("Error budget used", "percentunit"),
                   ov("Availability", [{"id": "decimals", "value": 2}]),
                   cell_color("Availability", [{"color": "red", "value": None}, {"color": "orange", "value": 0.99},
                                               {"color": "green", "value": 0.995}]),
                   ov("Error budget used", [{"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "lcd",
                                                                                    "valueDisplayMode": "text"}},
                                            {"id": "min", "value": 0}, {"id": "max", "value": 1},
                                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                                {"color": "green", "value": None}, {"color": "orange", "value": 0.5},
                                                {"color": "red", "value": 1}]}}]),
                   cell_color("P95 ms", [{"color": "green", "value": None}, {"color": "orange", "value": 300},
                                         {"color": "red", "value": 500}]),
                   ov("API", [{"id": "links", "value": API_LINK}])],
        desc="Objective: 99.5% non-5xx and P95 ≤ 500 ms over the selected range. 'Error budget used' = observed "
             "error rate / allowed error rate (0.5%); > 100% means the SLO is breached for that period."))
    P.append(ts(L, "Availability by API", [
        es("A", F + " AND NOT status:[500 TO 599]", terms="api-name", hide=True),
        es("B", terms="api-name", hide=True), math("C", "$A / $B")], w=12, unit="percentunit", minv=0.9, maxv=1,
        legend_calcs=("mean", "min"),
        overrides=[ov("C", [{"id": "displayName", "value": "${__field.labels[\"api-name\"]}"}], "byFrameRefID")]))
    P.append(ts(L, "P95 response time by API", [
        es("A", metrics=[pct("gateway-response-time-ms")], terms="api-name", alias="{{term api-name}}")], w=12,
        unit="ms", legend_calcs=("mean", "max"),
        overrides=[]))
    return dashboard("gravitee-slo", "Gravitee · SLOs & Error Budgets",
                     "Availability and latency objectives with multi-window burn-rate alerting (Prometheus) and "
                     "per-API SLO compliance (analytics).", P, common_vars(adhoc=False),
                     time_from="now-24h", refresh="1m")


SLO_HELP = """
**Objective:** 99.5 % of calls succeed (non-5xx) over 30 days → **error budget = 0.5 %**.

* **Gateway-wide SLIs & burn rates** come from Prometheus *recording rules*
  (`prometheus/rules/gravitee-slo.rules.yml`) — cheap to evaluate, alertable.
* **Alerts** use the Google SRE multi-window, multi-burn-rate pattern:
  *page* at 14.4× (5m & 1h), *ticket* at 6× (30m & 6h).
* **Per-API** compliance is computed from the analytics store, which knows
  the API, plan and application of every call.

Change the objective in the rules file and in the per-API table transformation.
"""


# ============================================================== 6. CALL LOGS
# Log index fields: entrypoint-* = between client and Gateway, endpoint-* = between Gateway and backend
LF = "api-name:$api AND (${search:raw})"   # raw: Search is a Lucene query, not a literal
# Call detail: the selected request id (escaped: ids contain hyphens); empty = the latest matching call
DF = LF + " AND request-id:*${request_id:lucene}*"
# Grafana regex strings take JS flags as /pattern/flags
SECRET_HEADERS = r"/\.(x-gravitee-api-key|authorization|proxy-authorization|cookie|set-cookie)$/i"
# Transpose turns the first field's value into the column header: lead with the request id, so the header
# names the call shown and no data row is lost
CALL_HEADER = organize(order=["request-id"])
ID_COL = "/[0-9a-f]{8}-[0-9a-f]{4}-/"
NOT_CALLED = ov("endpoint-response.status", [{"id": "mappings", "value": [{"type": "value", "options": {
    "0": {"text": "not called", "color": "transparent", "index": 0}}}]}])
LOG_COLS = {"@timestamp": "Time", "api-name": "API", "application-name": "Application",
            "entrypoint-request.method": "Method", "entrypoint-request.uri": "URI",
            "entrypoint-response.status": "Client status", "endpoint-response.status": "Backend status",
            "error-key": "Error key", "request-id": "Request id"}
LOG_STATUS = [cell_color("entrypoint-response.status", [{"color": "green", "value": None}, {"color": "orange", "value": 400},
                                                        {"color": "red", "value": 500}]),
              cell_color("endpoint-response.status", [{"color": "green", "value": None}, {"color": "orange", "value": 400},
                                                      {"color": "red", "value": 500}])]
# Time, URI and request id cells all open the call detail
CALL_DETAIL_URL = ("/d/gravitee-logs?${__url_time_range}&${ds_logs:queryparam}&${ds_analytics:queryparam}"
                   "&var-request_id=${__data.fields[\"request-id\"]}&var-search=*")
SELF_LINKS = [ov(f, [{"id": "links", "value": [{"title": "Show this call's headers and payloads",
                                               "url": CALL_DETAIL_URL}]}])
              for f in ("@timestamp", "entrypoint-request.uri", "request-id")]


def log_table(L, title, query, w=24, h=10, size=200, desc="", cols=LOG_COLS):
    return table(L, title, [es("A", query, metrics=[raw(size)], ts=False, ds=LOG)], w=w, h=h, desc=desc,
                 transformations=[{"id": "filterFieldsByName", "options": {"include": {"names": list(cols)}}},
                                  organize(cols, order=list(cols))],
                 overrides=LOG_STATUS + [NOT_CALLED] + SELF_LINKS)


def header_table(L, title, prefix, w=6, h=12):
    """One call's headers for one hop, as name/value rows; credentials are never displayed."""
    return table(L, title, [es("A", DF, metrics=[raw(1)], ts=False, ds=LOG)], w=w, h=h,
                 transformations=[
                     {"id": "filterFieldsByName", "options": {"include": {"pattern": rf"^(request-id|{prefix}\.headers\..*)$"}}},
                     {"id": "filterFieldsByName", "options": {"exclude": {"pattern": SECRET_HEADERS}}},
                     {"id": "renameByRegex", "options": {"regex": rf"^{prefix}\.headers\.(.*)$", "renamePattern": "$1"}},
                     CALL_HEADER,
                     {"id": "transpose", "options": {"firstFieldName": "Header", "restFieldsName": "Value"}}],
                 desc="Headers of the call selected by Request id (when empty, the most recent matching call). "
                      "API keys, Authorization and cookies are stored in the log index but hidden here.")


def logs():
    L = Layout()
    P = []
    P.append(text(L, "", LOGS_HELP, h=6))
    P.append(row(L, "Logged calls"))
    P.append(stat(L, "Logged calls", [es("A", LF, ds=LOG)], w=6, color_mode="background",
                  thresholds=[{"color": "#1f60c4", "value": None}]))
    P.append(stat(L, "Share of calls logged", [
        es("A", LF, ds=LOG, hide=True), es("B", F, hide=True),
        dict(reduce_("C", "A"), hide=True), dict(reduce_("D", "B"), hide=True), math("E", "$C / $D")],
        w=6, unit="percentunit", decimals=1, graph=False,
        desc="Logged calls / all calls in the analytics index. Logging conditions (sampling, a debug header…) and "
             "APIs without logging keep this below 100 %."))
    P.append(stat(L, "Logged failures (4xx/5xx)", [es("A", LF + " AND entrypoint-response.status:[400 TO 599]", ds=LOG)],
                  w=6, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]))
    P.append(stat(L, "Answered by the Gateway", [es("A", LF + " AND endpoint-response.status:0", ds=LOG)],
                  w=6, desc="Logged calls the Gateway answered without calling the backend (backend status 0): "
                            "rejected by plan security or a policy such as rate limiting."))
    P.append(ts(L, "Logged calls by client status", [
        es(r, LF + f" AND entrypoint-response.status:[{lo} TO {lo + 99}]", alias=a, ds=LOG)
        for r, lo, a in (("A", 200, "2xx"), ("B", 300, "3xx"), ("C", 400, "4xx"), ("D", 500, "5xx"))],
        w=12, stack=True, bars=True, overrides=STATUS_OVERRIDES, legend_calcs=("sum",)))
    P.append(bargauge(L, "Logged calls by API", [es("A", LF, terms="api-name", ts=False, ds=LOG)], w=6))
    P.append(bargauge(L, "Gateway rejections by error key", [
        es("A", LF + " AND _exists_:error-key", terms="error-key.keyword", ts=False, ds=LOG)], w=6,
        thresholds=[{"color": "orange", "value": None}]))

    P.append(row(L, "Call log explorer"))
    P.append(log_table(L, "Logged failed calls", LF + " AND entrypoint-response.status:[400 TO 599]", h=9,
                       desc="Click a request id to show its headers and payloads below."))
    P.append(log_table(L, "All logged calls", LF, h=9,
                       desc="Use Search to find calls by payload content with wildcards (e.g. *ORD-1234*, a customer or "
                            "order id) or by field. Click a request id to show the call below."))

    P.append(row(L, "Call detail"))
    P.append(table(L, "Call summary", [es("A", DF, metrics=[raw(1)], ts=False, ds=LOG)], w=8, h=16,
                   transformations=[
                       {"id": "filterFieldsByName", "options": {"include": {"names": [
                           "request-id", "@timestamp", "api-name", "application-name", "plan-id",
                           "entrypoint-request.method", "entrypoint-request.uri", "entrypoint-response.status",
                           "endpoint-request.method", "endpoint-request.uri", "endpoint-response.status",
                           "error-key", "error-message"]}}},
                       {"id": "convertFieldType", "options": {"conversions": [
                           {"targetField": "@timestamp", "destinationType": "string", "dateFormat": "YYYY-MM-DD HH:mm:ss.SSS"}]}},
                       organize({"@timestamp": "Time", "api-name": "API", "application-name": "Application",
                                 "plan-id": "Plan id",
                                 "entrypoint-request.method": "Client → Gateway method",
                                 "entrypoint-request.uri": "Client → Gateway URI",
                                 "entrypoint-response.status": "Gateway → client status",
                                 "endpoint-request.method": "Gateway → backend method",
                                 "endpoint-request.uri": "Gateway → backend URL",
                                 "endpoint-response.status": "Backend → Gateway status",
                                 "error-key": "Error key", "error-message": "Error message"},
                                order=["request-id", "@timestamp", "api-name", "application-name", "plan-id",
                                       "entrypoint-request.method", "entrypoint-request.uri", "endpoint-request.method",
                                       "endpoint-request.uri", "endpoint-response.status", "entrypoint-response.status",
                                       "error-key", "error-message"]),
                       {"id": "transpose", "options": {"firstFieldName": "Field", "restFieldsName": "Value"}}],
                   desc="The call selected by Request id; when it is empty, the most recent matching call."))
    P.append(table(L, "Payloads", [es("A", DF, metrics=[raw(1)], ts=False, ds=LOG)], w=16, h=16,
                   transformations=[
                       {"id": "filterFieldsByName", "options": {"include": {"names": [
                           "request-id", "entrypoint-request.body", "endpoint-request.body", "endpoint-response.body",
                           "entrypoint-response.body"]}}},
                       organize({"entrypoint-request.body": "1. Client → Gateway (request)",
                                 "endpoint-request.body": "2. Gateway → backend (request, after policies)",
                                 "endpoint-response.body": "3. Backend → Gateway (response)",
                                 "entrypoint-response.body": "4. Gateway → client (response, after policies)"},
                                order=["request-id", "entrypoint-request.body", "endpoint-request.body",
                                       "endpoint-response.body", "entrypoint-response.body"]),
                       {"id": "transpose", "options": {"firstFieldName": "Hop", "restFieldsName": "Body"}}],
                   overrides=[ov("Hop", [{"id": "custom.width", "value": 330}]),
                              ov(ID_COL, [{"id": "custom.cellOptions", "value": {"type": "auto", "wrapText": True}}],
                                 "byRegexp")],
                   desc="Bodies at each hop. Comparing 1↔2 or 3↔4 shows what policies changed (transformation, "
                        "masking). The Gateway caps logged bodies, so large payloads may be truncated."))
    P.append(header_table(L, "Client → Gateway headers", "entrypoint-request"))
    P.append(header_table(L, "Gateway → backend headers", "endpoint-request"))
    P.append(header_table(L, "Backend → Gateway headers", "endpoint-response"))
    P.append(header_table(L, "Gateway → client headers", "entrypoint-response"))

    v = [ds_var("ds_logs", "Call logs (Elasticsearch)", "elasticsearch", "gravitee-logs"),
         ds_var("ds_analytics", "Analytics (Elasticsearch)", "elasticsearch", "gravitee-analytics"),
         es_terms_var("api", "API", "api-name", ds=LOG),
         {"name": "request_id", "label": "Request id (call detail)", "type": "textbox", "query": "",
          "current": {"text": "", "value": ""}, "hide": 0},
         {"name": "search", "label": "Search payloads", "type": "textbox", "query": "*",
          "current": {"text": "*", "value": "*"}, "hide": 0}]
    return dashboard("gravitee-logs", "Gravitee · Call Logs",
                     "Headers and payloads of logged API calls, on the client and backend side: search them, list "
                     "failed calls, and inspect one call hop by hop.", P, v, time_from="now-1h")


LOGS_HELP = """
**Call logs** record the headers and payloads of API calls, on the client side and on the backend side. They're
written only for APIs with **logging enabled** (API → *Logs* → *Configure Reporting*), for the calls that match
its condition. The demo logs **1 call in 5** on every API. Logs are much bigger than analytics records,
so enable them per API, with a condition or for a limited time.

**Search** (Lucene) matches payloads with wildcards, e.g. `*wayne-enterprises*` or `*EUR*`, and any indexed field,
e.g. `entrypoint-response.status:503`, `error-key:RATE_LIMIT_TOO_MANY_REQUESTS` or `application-name:"Retail Web"`.
Headers are stored but not searchable. Click a **Request id** to show that call below, or open one from the
*Request id* links in **API Drill-down**. ⚠️ Logs can contain personal data and credentials: credential headers are
hidden here, but restrict access to this data source.
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    for name in os.listdir(OUT):
        if name.endswith(".json"):
            os.remove(os.path.join(OUT, name))
    for fn in (overview, api_detail, consumers, runtime, slo, logs):
        d = fn()
        for p in d["panels"]:
            # per-API ratios are meaningless on few-second buckets: at least 1 minute per point
            if p["title"] in ("Server error rate by API (5xx %)", "Availability by API"):
                p["interval"] = "1m"
        path = os.path.join(OUT, f"{d['uid']}.json")
        with open(path, "w") as f:
            json.dump(d, f, indent=2)
        print(f"{d['uid']:24} {sum(1 for p in d['panels'] if p['type'] != 'row'):3} panels")


if __name__ == "__main__":
    main()
