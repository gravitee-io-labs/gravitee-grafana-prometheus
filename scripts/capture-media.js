// Captures full-height PNGs of every Gravitee dashboard and records a walkthrough video (Grafana on :3000).
// Needs playwright-core (`npm i --no-save playwright-core@1.48.2`) and a local Google Chrome.
// Usage: node scripts/capture-media.js [shots|video|all]
//   FROM=<epoch ms | now-1h>  start of the time range (default now-1h); TO defaults to now
//   ONLY=<uid,uid>            capture only these dashboards
//   CALL_QUERY=<lucene>       logged call shown in Call Logs' call detail
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const MODE = process.argv[2] || 'all';
const ROOT = path.join(__dirname, '..');
const DASH_DIR = path.join(ROOT, 'grafana', 'dashboards');
const OUT = path.join(ROOT, 'media');
const GRAFANA = 'http://localhost:3000';
const RANGE = `from=${process.env.FROM || 'now-1h'}&to=${process.env.TO || 'now'}`;
const W = 1920;

const DASHBOARDS = [
  { uid: 'gravitee-overview', file: '1-api-program-overview', extra: '' },
  { uid: 'gravitee-api-detail', file: '2-api-drill-down', extra: '&var-api=Payments%20API' },
  { uid: 'gravitee-consumers', file: '3-consumers-plans-tenants', extra: '' },
  { uid: 'gravitee-runtime', file: '4-gateway-fleet-runtime', extra: '' },
  { uid: 'gravitee-slo', file: '5-slos-error-budgets', extra: '' },
  // Call detail shows a recent logged POST, so the payload panels have bodies on every hop
  { uid: 'gravitee-logs', file: '7-call-logs', extra: async () => `&var-request_id=${await recentCall(process.env.CALL_QUERY ||
    'entrypoint-request.method:POST AND _exists_:endpoint-response.body AND _exists_:application-name')}` },
].filter(d => !process.env.ONLY || process.env.ONLY.split(',').includes(d.uid));

// Request id of the most recent logged call matching a Lucene query
async function recentCall(query) {
  const r = await fetch(`${GRAFANA}/api/ds/query`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Basic ' + btoa('admin:admin') },
    body: JSON.stringify({ from: process.env.FROM || 'now-1h', to: process.env.TO || 'now', queries: [{
      refId: 'A', datasource: { type: 'elasticsearch', uid: 'gravitee-logs' }, query, timeField: '@timestamp',
      metrics: [{ id: '1', type: 'raw_data', settings: { size: '1' } }], bucketAggs: [] }] }) });
  const frame = (await r.json()).results.A.frames[0];
  const i = frame.schema.fields.findIndex(f => f.name === 'request-id');
  return frame.data.values[i][0];
}

// Client IPs are replaced by a placeholder in the page right before each screenshot
async function hideIps(page) {
  await page.evaluate(() => {
    const ip = /\b\d{1,3}(\.\d{1,3}){3}\b/g;
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n; (n = walk.nextNode());) if (ip.test(n.nodeValue)) n.nodeValue = n.nodeValue.replace(ip, '•.•.•.•');
  });
}

const url = (d, kiosk = true) => `${GRAFANA}/d/${d.uid}?orgId=1&${RANGE}${d.extra}${kiosk ? '&kiosk' : ''}&refresh=`;

// Grid height in px from the dashboard JSON (Grafana: 30px per unit + 8px gap)
function gridHeight(uid) {
  const d = JSON.parse(fs.readFileSync(path.join(DASH_DIR, `${uid}.json`), 'utf8'));
  const units = Math.max(...d.panels.map(p => p.gridPos.y + p.gridPos.h));
  return units * 38 + 160;
}

async function login(context) {
  const r = await context.request.post(`${GRAFANA}/login`, { data: { user: 'admin', password: 'admin' } });
  if (!r.ok()) throw new Error(`login failed: ${r.status()}`);
}

async function settle(page, ms = 6000) {
  await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
  // world map: wait for basemap tiles before capturing
  if (await page.locator('.ol-layer').count()) {
    await page.waitForFunction(() => performance.getEntriesByType('resource')
      .filter(e => e.name.includes('arcgisonline')).length >= 6, null, { timeout: 30000 }).catch(() => {});
  }
  await page.waitForTimeout(ms);
}

async function shots(browser) {
  for (const d of DASHBOARDS) {
    const h = gridHeight(d.uid);
    const ctx = await browser.newContext({ viewport: { width: W, height: h }, deviceScaleFactor: 1.5, colorScheme: 'dark' });
    await login(ctx);
    const page = await ctx.newPage();
    await page.goto(url(d));
    await settle(page, 9000);
    const file = path.join(OUT, `${d.file}.png`);
    await hideIps(page);
    await page.screenshot({ path: file });
    console.log(`png ${file} (${W}x${h})`);
    await ctx.close();
  }
  // hero image: the world map on its own
  if (DASHBOARDS[0].uid !== 'gravitee-overview') return;
  const ctx = await browser.newContext({ viewport: { width: W, height: 1080 }, deviceScaleFactor: 1.5, colorScheme: 'dark' });
  await login(ctx);
  const page = await ctx.newPage();
  const mapId = JSON.parse(fs.readFileSync(path.join(DASH_DIR, 'gravitee-overview.json'), 'utf8'))
    .panels.find(p => p.type === 'geomap').id;
  await page.goto(url(DASHBOARDS[0]) + `&viewPanel=panel-${mapId}`);
  await settle(page, 8000);
  await page.screenshot({ path: path.join(OUT, '6-calls-by-country-map.png') });
  console.log(`png ${path.join(OUT, '6-calls-by-country-map.png')}`);
  await ctx.close();
}

// Video = deterministic frames (one screenshot per scroll step, repeated frames for pauses) encoded with
// the system ffmpeg at FPS. Smooth regardless of machine speed; needs no Playwright video download.
const FPS = 25;
const TITLES = {
  'gravitee-overview': 'API Program Overview',
  'gravitee-api-detail': 'API Drill-down — Payments API',
  'gravitee-consumers': 'Consumers, Plans & Tenants',
  'gravitee-runtime': 'Gateway Fleet & Runtime',
  'gravitee-slo': 'SLOs & Error Budgets',
  'gravitee-logs': 'Call Logs — headers & payloads',
};

async function video(browser) {
  const { execFileSync } = require('child_process');
  const frames = path.join(OUT, '.video-tmp');
  fs.rmSync(frames, { recursive: true, force: true });
  fs.mkdirSync(frames, { recursive: true });
  let n = 0;
  let last = null;
  const frame = async page => {
    last = await page.screenshot({ type: 'jpeg', quality: 88 });
    fs.writeFileSync(path.join(frames, `${String(n++).padStart(6, '0')}.jpg`), last);
  };
  const hold = seconds => {
    for (let i = 0; i < Math.round(seconds * FPS); i++) {
      fs.writeFileSync(path.join(frames, `${String(n++).padStart(6, '0')}.jpg`), last);
    }
  };
  const caption = (page, text) => page.evaluate(t => {
    const el = document.createElement('div');
    el.id = 'capture-caption';
    el.textContent = t;
    Object.assign(el.style, { position: 'fixed', left: '50%', bottom: '28px', transform: 'translateX(-50%)',
      padding: '10px 22px', borderRadius: '8px', background: 'rgba(17,18,23,.88)', color: '#fff',
      font: '600 22px Inter, sans-serif', border: '1px solid rgba(255,255,255,.18)', zIndex: 99999 });
    document.body.appendChild(el);
  }, text);
  const uncaption = page => page.evaluate(() => document.getElementById('capture-caption')?.remove());

  const ctx = await browser.newContext({ viewport: { width: W, height: 1080 }, colorScheme: 'dark' });
  await login(ctx);
  const page = await ctx.newPage();
  for (const d of DASHBOARDS) {
    console.log(`recording ${d.uid}`);
    await page.goto(url(d));
    await settle(page, 5000);
    await caption(page, `Gravitee · ${TITLES[d.uid]}`);
    await frame(page);
    hold(3);
    await uncaption(page);
    await page.mouse.move(W / 2, 600);
    const scrollable = Math.max(0, gridHeight(d.uid) - 1080);
    const stages = Math.ceil(scrollable / 850);
    for (let s = 0; s < stages; s++) {
      const steps = Math.round(1.1 * FPS);
      for (let i = 0; i < steps; i++) {
        await page.mouse.wheel(0, scrollable / stages / steps);
        await page.waitForTimeout(25);
        await frame(page);
      }
      await page.waitForTimeout(1500);           // let lazily-loaded panels render, then pause on them
      await frame(page);
      hold(2.2);
    }
  }
  await ctx.close();
  const out = path.join(OUT, 'gravitee-dashboards-walkthrough.mp4');
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-framerate', String(FPS), '-i', path.join(frames, '%06d.jpg'),
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '20', '-preset', 'slow', '-movflags', '+faststart', out]);
  fs.rmSync(frames, { recursive: true, force: true });
  console.log(`video ${out} (${(n / FPS).toFixed(0)}s)`);
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  for (const d of DASHBOARDS) if (typeof d.extra === 'function') d.extra = await d.extra();
  const browser = await chromium.launch({ channel: 'chrome', headless: true,
    // Grafana's world map needs WebGL: use software rendering in headless Chrome
    args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  try {
    if (MODE === 'all' || MODE === 'shots') await shots(browser);
    if (MODE === 'all' || MODE === 'video') await video(browser);
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
