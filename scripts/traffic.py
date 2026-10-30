#!/usr/bin/env python3
"""Continuous, realistic demo traffic for the seeded APIs (see seed.py).

Mixes applications, plans, countries (X-Forwarded-For -> geo-IP), user agents, tenants,
successes, client errors, slow calls, bad/missing API keys and rate-limit bursts, on a healthy
baseline (~0.3% 5xx) with a periodic backend incident on one API (drives the SLO burn-rate alerts).
Usage: traffic.py [seconds]   (0 or no argument = run forever)
"""
import json, os, random, sys, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:28082")
RPS = float(os.environ.get("TRAFFIC_RPS", "15"))
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("TRAFFIC_SECONDS", "0"))

seed = json.load(open(os.environ.get("KEYS_FILE", os.path.join(HERE, ".keys.json"))))
KEYS, PATHS = seed["keys"], seed["apis"]

# Public IPs whose geo-IP resolves to distinct countries (ES ingest geoip)
IPS = {"US": ["8.8.8.8", "24.48.0.1", "52.94.236.248"], "GB": ["81.2.69.142", "51.140.0.1"],
       "FR": ["90.84.0.1", "193.252.0.1"], "DE": ["85.214.132.117", "62.157.0.1"],
       "BR": ["200.160.2.3"], "JP": ["133.242.0.1", "210.140.0.1"], "IN": ["49.44.0.1"],
       "AU": ["1.128.0.1"], "CA": ["24.114.0.1"], "SG": ["8.21.8.1"], "ZA": ["41.0.0.1"], "NL": ["145.100.0.1"]}
COUNTRY_WEIGHTS = {"US": 30, "GB": 12, "FR": 14, "DE": 10, "BR": 5, "JP": 6, "IN": 6, "AU": 3, "CA": 4, "SG": 3, "ZA": 2, "NL": 5}
AGENTS = {"Mobile Banking App": ["BankApp/5.2.1 (iPhone; iOS 18.1)", "BankApp/5.2.0 (Linux; Android 15; Pixel 9)"],
          "Partner Portal": ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"],
          "Retail Web": ["Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) Safari/605.1.15",
                         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/131.0"],
          "Internal BI": ["python-requests/2.32.3", "Apache-HttpClient/5.3 (Java/21)"],
          "Hackathon Sandbox": ["curl/8.7.1", "PostmanRuntime/7.42.0"],
          None: ["curl/8.7.1", "Go-http-client/2.0", "Mozilla/5.0 (X11; Linux x86_64) Chrome/129.0"]}
TENANTS = ["acme-bank", "globex", "initech", "umbrella", "wayne-enterprises"]
CHANNELS = ["mobile", "web", "partner", "batch"]
APP_WEIGHTS = {"Mobile Banking App": 35, "Retail Web": 25, "Partner Portal": 15, "Internal BI": 10, "Hackathon Sandbox": 5}

# (weight, method, backend path) – httpbin semantics
OPS = [(55, "GET", "/get"), (12, "POST", "/anything/transactions"), (6, "GET", "/delay/0.4"),
       (2, "GET", "/delay/1.2"), (6, "GET", "/status/404"), (3, "PUT", "/status/400"),
       (0.15, "GET", "/status/500"), (0.1, "GET", "/status/503"), (0.05, "GET", "/status/502"),
       (5, "GET", "/anything/items/42"), (3, "DELETE", "/anything/items/42"), (2, "PATCH", "/anything/items/42")]

stats = {"sent": 0}
# Periodic incident: one API's backend degrades (503s + slowness) for INCIDENT_SECONDS every INCIDENT_EVERY seconds
INCIDENT_EVERY = int(os.environ.get("INCIDENT_EVERY", "600"))
INCIDENT_SECONDS = int(os.environ.get("INCIDENT_SECONDS", "120"))
incident = {"api": None, "until": 0.0}
lock = threading.Lock()


def pick(weights):
    return random.choices(list(weights), weights=list(weights.values()))[0]


def send(method, url, headers):
    data = b'{"amount": 42.0, "currency": "EUR"}' if method in ("POST", "PUT", "PATCH") else None
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass
    with lock:
        stats["sent"] += 1


def one_request():
    r = random.random()
    country = pick(COUNTRY_WEIGHTS)
    headers = {"X-Forwarded-For": random.choice(IPS[country]), "Content-Type": "application/json",
               "X-Tenant-ID": random.choice(TENANTS), "X-Channel": random.choice(CHANNELS)}
    _, method, path = random.choices(OPS, weights=[o[0] for o in OPS])[0]
    if r < 0.12:                                    # public keyless API
        api, app = "Product Catalog API", None
    elif r < 0.15:                                  # wrong / missing credentials
        app = None
        api = random.choice(["Payments API", "Orders API", "Customer Profile API"])
        if random.random() < 0.5:
            headers["X-Gravitee-Api-Key"] = "invalid-" + str(random.randint(1000, 9999))
    else:
        app = pick(APP_WEIGHTS)
        api = random.choice(list(KEYS[app]))
        headers["X-Gravitee-Api-Key"] = KEYS[app][api]
    headers["User-Agent"] = random.choice(AGENTS[app])
    if api == incident["api"] and time.time() < incident["until"]:
        path = random.choice(["/status/503", "/status/503", "/status/500", "/delay/1.5", "/get"])
    send(method, f"{GATEWAY}{PATHS[api]}{path}", headers)


def burst():
    """Free-plan bursts that trip the 3 req/s rate limit -> 429 RATE_LIMIT_TOO_MANY_REQUESTS."""
    app = random.choice(["Hackathon Sandbox", "Partner Portal"])
    headers = {"X-Gravitee-Api-Key": KEYS[app]["Payments API"], "User-Agent": random.choice(AGENTS[app]),
               "X-Forwarded-For": random.choice(IPS["IN"] + IPS["BR"]), "X-Tenant-ID": "initech", "X-Channel": "batch"}
    for _ in range(random.randint(8, 20)):
        threading.Thread(target=send, args=("GET", f"{GATEWAY}{PATHS['Payments API']}/get", headers), daemon=True).start()


def main():
    start = time.time()
    next_burst = start + random.uniform(10, 30)
    next_incident = start + random.uniform(60, 180)
    print(f"traffic -> {GATEWAY} at ~{RPS} req/s" + (f" for {DURATION}s" if DURATION else " (forever)"), flush=True)
    while not DURATION or time.time() - start < DURATION:
        # gentle sine wave so charts look alive (±40%)
        rate = RPS * (1 + 0.4 * __import__("math").sin((time.time() - start) / 60))
        threading.Thread(target=one_request, daemon=True).start()
        if INCIDENT_EVERY and time.time() > next_incident:
            incident["api"] = random.choice(["Payments API", "Orders API", "Customer Profile API"])
            incident["until"] = time.time() + INCIDENT_SECONDS
            next_incident = time.time() + INCIDENT_EVERY * random.uniform(0.8, 1.2)
            print(f"incident: {incident['api']} degraded for {INCIDENT_SECONDS}s", flush=True)
        if time.time() > next_burst:
            burst()
            next_burst = time.time() + random.uniform(20, 60)
        time.sleep(random.expovariate(rate))
        if stats["sent"] and stats["sent"] % 1000 == 0:
            print(f"{stats['sent']} requests sent", flush=True)
    time.sleep(2)
    print(f"done: {stats['sent']} requests", flush=True)


if __name__ == "__main__":
    main()
