// Captures full-height PNGs of every Gravitee OTel dashboard (Grafana on :3001) into otel/media/.
// Needs playwright-core (`(cd scripts && npm i --no-save playwright-core@1.48.2)`) and a local Google Chrome.
// Usage: node otel/capture-media.js     FROM=<epoch ms | now-1h> (default now-1h)   TO=<epoch ms | now>   ONLY=<uid,uid>
const fs = require('fs');
const path = require('path');
const { chromium } = require(path.join(__dirname, '..', 'scripts', 'node_modules', 'playwright-core'));

const DASH_DIR = path.join(__dirname, 'grafana', 'dashboards');
const OUT = path.join(__dirname, 'media');
const GRAFANA = 'http://localhost:3001';
const AUTH = 'Basic ' + btoa('admin:admin');
const RANGE = `from=${process.env.FROM || 'now-1h'}&to=${process.env.TO || 'now'}`;
const W = 1920;

const DASHBOARDS = [
  { uid: 'gravitee-otel-overview', file: '1-api-program-overview', extra: '' },
  { uid: 'gravitee-otel-api', file: '2-api-drill-down', extra: '&var-api=Payments%20API' },
  { uid: 'gravitee-otel-consumers', file: '3-consumers-plans', extra: '' },
  { uid: 'gravitee-otel-runtime', file: '4-gateway-fleet-runtime', extra: '' },
  { uid: 'gravitee-otel-slo', file: '5-slos-error-budgets', extra: '' },
  // Call detail shows a recent logged POST, so the payload panel has a body on every hop
  { uid: 'gravitee-otel-logs', file: '6-call-logs', extra: async () => `&var-request_id=${await recentCall()}&var-search=EUR` },
].filter(d => !process.env.ONLY || process.env.ONLY.split(',').includes(d.uid));

// Request id of the most recent logged POST with a backend response
async function recentCall() {
  const q = '{service_name="gio-apim-gateway"} | gravitee_log_type="endpoint-response" | gravitee_api_name="Payments API"';
  const end = /^\d+$/.test(process.env.TO || '') ? Number(process.env.TO) : Date.now();
  const r = await fetch(`${GRAFANA}/api/datasources/proxy/uid/gravitee-loki/loki/api/v1/query_range?` +
    new URLSearchParams({ query: q, limit: '20', start: String((end - 600000) * 1e6), end: String(end * 1e6) }),
    { headers: { Authorization: AUTH } });
  const streams = (await r.json()).data.result;
  return streams[0].stream.gravitee_request_id;
}

// Client IPs are replaced by a placeholder in the page right before each screenshot
async function hideIps(page) {
  await page.evaluate(() => {
    const ip = /\b\d{1,3}(\.\d{1,3}){3}\b/g;
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n; (n = walk.nextNode());) if (ip.test(n.nodeValue)) n.nodeValue = n.nodeValue.replace(ip, '•.•.•.•');
  });
}

function gridHeight(uid) {
  const d = JSON.parse(fs.readFileSync(path.join(DASH_DIR, `${uid}.json`), 'utf8'));
  return Math.max(...d.panels.map(p => p.gridPos.y + p.gridPos.h)) * 38 + 160;
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  for (const d of DASHBOARDS) {
    const extra = typeof d.extra === 'function' ? await d.extra() : d.extra;
    const ctx = await browser.newContext({ viewport: { width: W, height: gridHeight(d.uid) }, deviceScaleFactor: 1.5,
                                           colorScheme: 'dark' });
    const r = await ctx.request.post(`${GRAFANA}/login`, { data: { user: 'admin', password: 'admin' } });
    if (!r.ok()) throw new Error(`login failed: ${r.status()}`);
    const page = await ctx.newPage();
    await page.goto(`${GRAFANA}/d/${d.uid}?orgId=1&${RANGE}${extra}&kiosk&refresh=`);
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(8000);
    await hideIps(page);
    await page.screenshot({ path: path.join(OUT, `${d.file}.png`), fullPage: true });
    console.log(`${d.file}.png`);
    await ctx.close();
  }
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
