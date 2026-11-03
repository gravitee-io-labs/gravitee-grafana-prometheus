#!/usr/bin/env bash
# Builds the OpenTelemetry (Elasticsearch-free) dashboards and packages them, with collector, data source and
# rule files, into dist/. Separate from the Elasticsearch pack (dashboards-src/package.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

python3 otel/build.py
NAME=gravitee-grafana-dashboards-otel
OUT=dist/$NAME
rm -rf "$OUT" "dist/$NAME.zip"
mkdir -p "$OUT/dashboards" "$OUT/prometheus-rules" "$OUT/collector" "$OUT/grafana-provisioning/datasources" \
         "$OUT/grafana-provisioning/dashboards"

cp otel/grafana/dashboards/gravitee-otel-*.json "$OUT/dashboards/"
cp otel/prometheus/rules/*.yml "$OUT/prometheus-rules/"
sed -n '/^scrape_configs:/,$p' otel/prometheus/prometheus.yml > "$OUT/prometheus-rules/scrape-config.example.yml"
cp otel/collector.yaml otel/tempo.yaml otel/loki.yaml "$OUT/collector/"
cp otel/grafana/provisioning/datasources/*.yml "$OUT/grafana-provisioning/datasources/"
cp otel/grafana/provisioning/dashboards/*.yml "$OUT/grafana-provisioning/dashboards/"
cp otel/INSTALL.md "$OUT/README.md"
cp LICENSE "$OUT/LICENSE"
mkdir -p "$OUT/media"
cp otel/media/*.png "$OUT/media/" 2>/dev/null || echo "warning: no screenshots in otel/media/ (run otel/capture-media.js)" >&2

# Never ship a license: refuse to package if any license material slipped in
if grep -rqiE 'gravitee_license_key|license[_-]?key|licenseSignature' "$OUT"; then
  echo "ERROR: license material found in $OUT — refusing to package" >&2
  rm -rf "$OUT"
  exit 1
fi
if [ -f .secrets/license.env ] && grep -rqF "$(cut -d= -f2- .secrets/license.env | cut -c1-40)" "$OUT"; then
  echo "ERROR: the local license key was found in $OUT — refusing to package" >&2
  rm -rf "$OUT"
  exit 1
fi

# Optional local denylist (one extended regex per line, git-ignored): words that must never ship in the pack
DENY=dashboards-src/.denylist
if [ -s "$DENY" ] && grep -rliEf "$DENY" --include='*.md' --include='*.json' --include='*.yml' --include='*.yaml' "$OUT"; then
  echo "ERROR: a $DENY pattern was found in the files above — refusing to package" >&2
  rm -rf "$OUT"
  exit 1
fi

(cd dist && zip -qr "$NAME.zip" "$NAME")
echo "dist/$NAME.zip"
