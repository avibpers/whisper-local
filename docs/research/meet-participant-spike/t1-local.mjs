// t1-local.mjs  (THROWAWAY SPIKE)
// Test 1, no meeting needed: does a page that asks for the camera get Koli's
// drawn board, do frames keep going out through a real WebRTC connection,
// and what does it cost? One run per browser and window mode.
//
//   node t1-local.mjs <chrome|chromium> <headless|offscreen|minimized> [seconds] [tickMs]

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { launch, sleep } from './cdp.mjs';

const here = path.dirname(new URL(import.meta.url).pathname.slice(1));
const [binary = 'chrome', mode = 'headless', secondsArg = '30', tickArg = '1000'] = process.argv.slice(2);
const seconds = Number(secondsArg);
const tickMs = Number(tickArg);

// The test page must come from http://127.0.0.1 (a secure origin, like a real call page).
const server = http.createServer((req, res) => {
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(fs.readFileSync(path.join(here, 'call-test.html')));
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const url = `http://127.0.0.1:${server.address().port}/`;

const inject = fs.readFileSync(path.join(here, 'camera-inject.js'), 'utf8')
  .replace(/\/\*CONFIG\*\/.*\/\*END\*\//, JSON.stringify({ width: 1280, height: 720, tickMs }));

const result = { binary, mode, seconds, tickMs };
const browser = await launch({
  binary, profileName: `t1-${binary}-${mode}-${tickMs}`,
  mode: mode === 'headless' ? 'headless' : 'offscreen',
});
try {
  const { targetId, sessionId } = await browser.page();
  result.version = (await browser.send('Browser.getVersion')).product;
  await browser.send('Page.addScriptToEvaluateOnNewDocument', { source: inject }, sessionId);
  await browser.send('Page.navigate', { url }, sessionId);
  for (let i = 0; i < 50; i++) {
    await sleep(200);
    const state = await browser.evaluate(sessionId, 'window.__call && (window.__call.error || window.__call.ready)');
    if (state) break;
  }
  result.error = await browser.evaluate(sessionId, 'window.__call.error || null');
  result.tracks = await browser.evaluate(sessionId, 'window.__call.tracks');
  result.devices = await browser.evaluate(sessionId, 'window.__call.devices');

  if (mode === 'minimized') {
    const { windowId } = await browser.send('Browser.getWindowForTarget', { targetId });
    await browser.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'minimized' } });
    await sleep(1000);
  }
  await browser.evaluate(sessionId, `window.__koli.board({ subtitle: 'Test ${binary} ${mode}', lines: ['Now: pricing for the Pro plan', 'Decided: ship on Thursday', 'Task: Eran sends the QA list'] })`);
  await sleep(3000);   // let the connection settle before measuring

  const before = await browser.evaluate(sessionId, 'window.__call.stats()');
  const cpuBefore = await browser.cpuSeconds();
  const samples = [];
  const started = Date.now();
  const step = Math.max(10, Math.round(seconds / 6));
  while ((Date.now() - started) / 1000 < seconds) {
    await sleep(step * 1000);
    const s = await browser.evaluate(sessionId, 'window.__call.stats()');
    samples.push({ at: Math.round((Date.now() - started) / 1000), sent: s.sent - before.sent, decoded: s.decoded - before.decoded });
  }
  const after = await browser.evaluate(sessionId, 'window.__call.stats()');
  const cpuAfter = await browser.cpuSeconds();
  const elapsed = (Date.now() - started) / 1000;
  const info = await browser.evaluate(sessionId, 'window.__koli.info()');

  result.framesSent = after.sent - before.sent;
  result.framesDecoded = after.decoded - before.decoded;
  result.fps = +(result.framesSent / elapsed).toFixed(2);
  result.kbps = +(((after.bytesSent - before.bytesSent) * 8) / elapsed / 1000).toFixed(1);
  result.size = `${after.width}x${after.height}`;
  result.codec = after.codec;
  result.connection = after.state;
  result.cpuPercentOfOneCore = +(((cpuAfter - cpuBefore) / elapsed) * 100).toFixed(1);
  result.memory = browser.memory();
  result.samples = samples;
  result.hidden = info.hidden;
  result.visibility = info.visibility;
  result.webdriver = info.webdriver;
  result.headlessInUA = /Headless/.test(info.ua);
  result.cameraCalls = info.calls.length;
  try {
    result.screenshot = await browser.screenshot(sessionId, path.join(here, `t1-${binary}-${mode}.png`));
  } catch (e) {
    result.screenshot = `none (${e.message})`;
  }
} catch (e) {
  result.failed = String(e.message || e);
} finally {
  await browser.close();
  server.close();
}
console.log(JSON.stringify(result, null, 1));
