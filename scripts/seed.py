#!/usr/bin/env python3
"""Seeds a realistic demo estate so every dashboard panel has data.

APIs, plans (API key / keyless, with rate limits), applications, subscriptions, and
policy-assign-metrics stamps (custom.app / custom.plan / custom.tenant / custom.channel),
and call logging (headers + payloads) on a 1-in-5 sample of calls.
With OTEL=1 (the Elasticsearch-free stack in otel/), every API also sends OpenTelemetry traces and logs.
Idempotent: existing APIs / applications (matched by name) are reused.
Writes the API keys to scripts/.keys.json (or KEYS_FILE) for the traffic generator.
"""
import base64, json, os, sys, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MGMT = os.environ.get("MGMT_URL", "http://localhost:28083/management")
V1 = f"{MGMT}/organizations/DEFAULT/environments/DEFAULT"
V2 = f"{MGMT}/v2/environments/DEFAULT"
AUTH = "Basic " + base64.b64encode(os.environ.get("MGMT_AUTH", "admin:admin").encode()).decode()
BACKEND = os.environ.get("BACKEND_URL", "http://backend:8080")
# policy-assign-metrics is an Enterprise feature: only stamp custom.* dimensions when licensed
ENTERPRISE = os.environ.get("ENTERPRISE", "0") == "1"
# OpenTelemetry route: traces on every call (the source of the per-API metrics) and OTel logs of logged calls
OTEL = os.environ.get("OTEL", "0") == "1"
KEYS_FILE = os.environ.get("KEYS_FILE", os.path.join(HERE, ".keys-otel.json" if OTEL else ".keys.json"))


def call(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": AUTH, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} -> {e.code}: {e.read().decode()[:500]}")


def assign_metrics(*metrics):
    return {"name": "Stamp analytics dimensions", "enabled": True, "policy": "policy-assign-metrics",
            "configuration": {"scope": "REQUEST", "metrics": [{"name": n, "value": v} for n, v in metrics]}}


def rate_limit(per_second):
    return {"name": f"Rate limit {per_second}/s", "enabled": True, "policy": "rate-limit",
            "configuration": {"async": False, "addHeaders": True,
                              "rate": {"useKeySpEL": False, "key": "", "limit": per_second,
                                       "periodTime": 1, "periodTimeUnit": "SECONDS"}}}


def flow(name, steps):
    return {"name": name, "enabled": True,
            "selectors": [{"type": "HTTP", "path": "/", "pathOperator": "STARTS_WITH", "methods": []}],
            "request": steps, "response": [], "subscribe": [], "publish": []}


# Never-null EL (a null value silently drops the custom metric)
APP_EL = "{#subscription != null && #subscription.applicationName != null ? #subscription.applicationName : 'Anonymous (keyless)'}"
TENANT_EL = "{#request.headers['X-Tenant-ID'] != null ? #request.headers['X-Tenant-ID'][0] : 'unknown'}"
CHANNEL_EL = "{#request.headers['X-Channel'] != null ? #request.headers['X-Channel'][0] : 'direct'}"

# Call logging (headers + payloads, client and backend side) for the Call Logs dashboard.
# The condition runs when the request arrives, so it cannot select on the response status:
# log a stable 1-in-5 sample of calls (~2 KB each) instead of every call.
LOGGING = {"mode": {"entrypoint": True, "endpoint": True}, "phase": {"request": True, "response": True},
           "content": {"headers": True, "payload": True},
           "condition": "{#request.id.hashCode() % 5 == 0}"}
ANALYTICS = {"enabled": True, "logging": LOGGING}
if OTEL:
    ANALYTICS |= {"tracing": {"enabled": True, "verbose": False}, "otelLogs": {"enabled": True}}
# Fields accepted by PUT /v2/apis/{id}
UPDATABLE = ["name", "apiVersion", "definitionVersion", "type", "description", "listeners", "endpointGroups",
             "flows", "flowExecution", "analytics", "properties", "resources", "responseTemplates", "labels",
             "tags", "categories", "groups", "visibility", "lifecycleState", "disableMembershipNotifications",
             "allowMultiJwtOauth2Subscriptions", "failover", "services"]

APIS = [
    {"name": "Payments API", "path": "/payments", "version": "2.1", "plans": [
        {"name": "Gold", "security": "API_KEY", "rps": 200},
        {"name": "Free", "security": "API_KEY", "rps": 3},
    ]},
    {"name": "Orders API", "path": "/orders", "version": "1.4", "plans": [
        {"name": "Partner", "security": "API_KEY", "rps": 50},
    ]},
    {"name": "Customer Profile API", "path": "/customers", "version": "3.0", "plans": [
        {"name": "Internal", "security": "API_KEY", "rps": 100},
    ]},
    {"name": "Product Catalog API", "path": "/catalog", "version": "1.0", "plans": [
        {"name": "Public", "security": "KEY_LESS", "rps": 30},
    ]},
]

APPS = {
    "Mobile Banking App": [("Payments API", "Gold"), ("Customer Profile API", "Internal")],
    "Partner Portal": [("Orders API", "Partner"), ("Payments API", "Free")],
    "Retail Web": [("Payments API", "Gold"), ("Orders API", "Partner")],
    "Internal BI": [("Customer Profile API", "Internal"), ("Orders API", "Partner")],
    "Hackathon Sandbox": [("Payments API", "Free")],
}


def find_api(name):
    res = call("POST", f"{V2}/apis/_search", {"query": name})["data"]
    return next((a for a in res if a["name"] == name), None)


def ensure_api(spec):
    api = find_api(spec["name"])
    if not api:
        api = call("POST", f"{V2}/apis", {
            "name": spec["name"], "apiVersion": spec["version"], "definitionVersion": "V4", "type": "PROXY",
            "description": f"Demo {spec['name']} for the Gravitee Grafana dashboards",
            "listeners": [{"type": "HTTP", "paths": [{"path": spec["path"]}], "entrypoints": [{"type": "http-proxy"}]}],
            "endpointGroups": [{"name": "default", "type": "http-proxy", "endpoints": [
                {"name": "backend", "type": "http-proxy", "configuration": {"target": BACKEND}}]}],
            "flows": [flow("Analytics dimensions", [assign_metrics(("app", APP_EL), ("tenant", TENANT_EL),
                                                                    ("channel", CHANNEL_EL))])] if ENTERPRISE else [],
            "analytics": ANALYTICS,
        })
        print(f"created API {spec['name']}")
    else:
        ensure_logging(api)
    plans = {p["name"]: p for p in call("GET", f"{V2}/apis/{api['id']}/plans?perPage=100")["data"]}
    for p in spec["plans"]:
        if p["name"] not in plans:
            plan = call("POST", f"{V2}/apis/{api['id']}/plans", {
                "name": p["name"], "definitionVersion": "V4", "mode": "STANDARD", "validation": "AUTO",
                "security": {"type": p["security"]},
                "flows": [flow(f"{p['name']} plan",
                                ([assign_metrics(("plan", p["name"]))] if ENTERPRISE else []) + [rate_limit(p["rps"])])],
            })
            call("POST", f"{V2}/apis/{api['id']}/plans/{plan['id']}/_publish")
            plans[p["name"]] = plan
            print(f"  plan {p['name']} ({p['security']}, {p['rps']} rps)")
    if api.get("state") != "STARTED":
        call("POST", f"{V2}/apis/{api['id']}/_start")
    call("POST", f"{V2}/apis/{api['id']}/deployments", {"deploymentLabel": "seed"})
    return api, plans


def ensure_logging(api):
    full = call("GET", f"{V2}/apis/{api['id']}")
    analytics = full.get("analytics", {})
    if all(analytics.get(k) == v for k, v in ANALYTICS.items() if k != "enabled"):
        return
    body = {k: full[k] for k in UPDATABLE if k in full}
    body["analytics"] = {**analytics, **ANALYTICS}
    call("PUT", f"{V2}/apis/{api['id']}", body)
    print(f"  call logging enabled on {api['name']} (1 call in 5)" + (", with OpenTelemetry traces and logs" if OTEL else ""))


def ensure_app(name):
    apps = call("GET", f"{V1}/applications?query={urllib.parse.quote(name)}") or []
    app = next((a for a in apps if a["name"] == name), None)
    if not app:
        app = call("POST", f"{V1}/applications", {"name": name, "description": f"{name} (demo)",
                                                 "settings": {"app": {"type": "web"}}})
        print(f"created application {name}")
    return app


def delete_api(name):
    api = find_api(name)
    if api:
        if api.get("state") == "STARTED":
            call("POST", f"{V2}/apis/{api['id']}/_stop")
        call("DELETE", f"{V2}/apis/{api['id']}?closePlans=true")
        print(f"deleted API {name}")


def main():
    # --recreate rebuilds the demo APIs, e.g. to add the Enterprise stamps after mounting a license
    if "--recreate" in sys.argv:
        for spec in APIS:
            delete_api(spec["name"])
    apis = {s["name"]: ensure_api(s) for s in APIS}
    keys = {}
    for app_name, subs in APPS.items():
        app = ensure_app(app_name)
        for api_name, plan_name in subs:
            api, plans = apis[api_name]
            plan = plans[plan_name]
            existing = call("GET", f"{V2}/apis/{api['id']}/subscriptions?applicationIds={app['id']}&perPage=100")["data"]
            sub = next((s for s in existing if s["plan"]["id"] == plan["id"] and s["status"] == "ACCEPTED"), None)
            if not sub:
                sub = call("POST", f"{V2}/apis/{api['id']}/subscriptions", {"applicationId": app["id"], "planId": plan["id"]})
                print(f"  {app_name} -> {api_name}/{plan_name}")
            api_keys = call("GET", f"{V2}/apis/{api['id']}/subscriptions/{sub['id']}/api-keys")["data"]
            keys.setdefault(app_name, {})[api_name] = api_keys[0]["key"]
    out = {"keys": keys, "apis": {n: s["path"] for n, s in zip(apis, APIS)}}
    with open(KEYS_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {len(keys)} applications' keys to {os.path.relpath(KEYS_FILE)}")


if __name__ == "__main__":
    import urllib.parse
    main()
