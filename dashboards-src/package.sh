#!/usr/bin/env bash
# Builds the dashboards and packages them, with data source + rule files, into dist/
set -euo pipefail
cd "$(dirname "$0")/.."

python3 dashboards-src/build.py
NAME=gravitee-grafana-dashboards
OUT=dist/$NAME
rm -rf "$OUT" "dist/$NAME.zip"
mkdir -p "$OUT/dashboards" "$OUT/prometheus-rules" "$OUT/grafana-provisioning/datasources" "$OUT/grafana-provisioning/dashboards"

cp grafana/dashboards/gravitee-*.json "$OUT/dashboards/"
cp prometheus/rules/*.yml "$OUT/prometheus-rules/"
cp grafana/provisioning/datasources/*.yml "$OUT/grafana-provisioning/datasources/"
cp grafana/provisioning/dashboards/gravitee.yml "$OUT/grafana-provisioning/dashboards/"
cp dashboards-src/INSTALL.md "$OUT/README.md"
cp dashboards-src/GUIDE.html "$OUT/GUIDE.html"
cp LICENSE "$OUT/LICENSE"
mkdir -p "$OUT/media"
cp media/*.png "$OUT/media/" 2>/dev/null || echo "warning: no screenshots in media/ (run scripts/capture-media.js)" >&2
cp media/*.mp4 "$OUT/media/" 2>/dev/null || echo "warning: no video in media/" >&2
sed -n '/^scrape_configs:/,$p' prometheus/prometheus.yml > "$OUT/prometheus-rules/scrape-config.example.yml"

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
if [ -s "$DENY" ] && grep -rliEf "$DENY" --include='*.html' --include='*.md' --include='*.json' --include='*.yml' "$OUT"; then
  echo "ERROR: a $DENY pattern was found in the files above — refusing to package" >&2
  rm -rf "$OUT"
  exit 1
fi

(cd dist && zip -qr "$NAME.zip" "$NAME")
echo "dist/$NAME.zip"
