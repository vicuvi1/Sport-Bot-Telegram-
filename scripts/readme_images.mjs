// Regenerates the README images with headless Chrome (Node 22+, no npm packages).
//
//   1. python -m webapp.dev --reset          (the app with sample data, port 8090)
//   2. node scripts/readme_images.mjs        (phone screenshots + hero + strips)
//
// Env: CHROME (path to chrome/msedge), APP (dev server URL, default http://localhost:8090).
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const assets = join(root, 'docs', 'assets');
const chrome = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const app = (process.env.APP || 'http://localhost:8090') + '/?banner=0';
const port = 9333;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const proc = spawn(chrome, ['--headless=new', '--disable-gpu', '--hide-scrollbars', `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), 'readme-'))}`, '--allow-file-access-from-files', 'about:blank'], { stdio: 'ignore' });

let target;
for (let i = 0; i < 50 && !target; i++) {
  await sleep(200);
  try { target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === 'page'); } catch { /* starting */ }
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r));
let id = 0;
const pending = new Map();
ws.addEventListener('message', (e) => {
  const msg = JSON.parse(e.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
});
const send = (method, params = {}) => new Promise((done) => { const n = ++id; pending.set(n, done); ws.send(JSON.stringify({ id: n, method, params })); });
const save = (file, shot) => { writeFileSync(file, Buffer.from(shot.result.data, 'base64')); console.log('wrote', file); };

// 1. The app on an emulated phone (390x844 @3x), animations settled.
await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 3, mobile: true });
await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
for (const screen of ['home', 'today', 'duel', 'chat', 'live', 'player', 'report', 'progress']) {
  await send('Page.navigate', { url: screen === 'home' ? app : `${app}&screen=${screen}` });
  await sleep(3500);
  save(join(assets, 'app', `${screen}.png`), await send('Page.captureScreenshot', { format: 'png' }));
}

// 2. Banner and strips: HTML pages around those screenshots, transparent corners.
await send('Emulation.setDeviceMetricsOverride', { width: 1400, height: 1000, deviceScaleFactor: 2, mobile: false });
await send('Emulation.setDefaultBackgroundColorOverride', { color: { r: 0, g: 0, b: 0, a: 0 } });
const pages = {
  'hero.png': 'hero.html',
  'train.png': 'strip.html?s=today,player,live&t=One-tap <i>logging</i>|Guided <i>workout</i>|Train <i>live</i>' +
    '&d=Combos, confetti, sounds.<br>Undo if you fat-finger it.|Animated demo, giant %2B1,<br>rest timer that beeps.|Both of you, side by side,<br>updating every 2 seconds.',
  'compete.png': 'strip.html?s=duel,chat,report&t=Duel <i>arena</i>|Crew <i>chat</i>|Report <i>card</i>' +
    '&d=Your reps drain his HP.<br>Roasts fly as fireballs.|Stickers, quick replies,<br>proof clips: legit or cap?|Your month in one image,<br>also sent on the 1st.',
};
for (const [out, page] of Object.entries(pages)) {
  const cut = page.indexOf('?');
  const [file, query] = cut < 0 ? [page, ''] : [page.slice(0, cut), page.slice(cut + 1)];
  await send('Page.navigate', { url: pathToFileURL(join(assets, 'src', file)).href + (query ? '?' + query : '') });
  await sleep(2500);
  const box = await send('Runtime.evaluate', {
    expression: 'JSON.stringify((document.querySelector(".card")).getBoundingClientRect())', returnByValue: true });
  const r = JSON.parse(box.result.result.value);
  save(join(assets, out), await send('Page.captureScreenshot', {
    format: 'png', clip: { x: r.x, y: r.y, width: r.width, height: r.height, scale: 1 } }));
}

ws.close();
proc.kill();
process.exit(0);
