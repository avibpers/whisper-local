// t2-meet.mjs  (THROWAWAY SPIKE)
// Test 2, a real Meet call: keeps one browser open with Koli's camera in
// place and takes commands on a local port, so each step (look, type, click,
// change the board, measure) can be done by hand and its result seen.
//
//   node t2-meet.mjs <chrome|chromium> <headless|offscreen|window> <name>
//
// Commands (curl http://127.0.0.1:<port in t2-<name>.port>/...):
//   /goto?url=   /shot?name=   /eval (POST js)   /text (POST text)   /key?k=Enter
//   /click?x=&y=   /board (POST json)   /stats   /minimize   /restore   /close

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { launch, sleep } from './cdp.mjs';

const here = path.dirname(new URL(import.meta.url).pathname.slice(1));
const [binary = 'chrome', mode = 'offscreen', name = 'meet'] = process.argv.slice(2);

const KEYS = {
  Enter: { key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r' },
  Tab: { key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 },
  Escape: { key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 },
};

const inject = fs.readFileSync(path.join(here, 'camera-inject.js'), 'utf8');
// A plain Chrome user agent in headless, where Chrome would say "HeadlessChrome".
const extraArgs = (process.env.KOLI_ARGS || '').split(' ').filter(Boolean);
const browser = await launch({ binary, profileName: `t2-${name}`, mode, extraArgs, size: process.env.KOLI_SIZE || '1100,760' });
const { targetId, sessionId } = await browser.page();
const version = await browser.send('Browser.getVersion');
if (/Headless/.test(version.userAgent)) {
  const major = (version.product.match(/\/(\d+)/) || [])[1] || '157';
  await browser.send('Emulation.setUserAgentOverride', {
    userAgent: version.userAgent.replace('HeadlessChrome', 'Chrome'),
    userAgentMetadata: {
      brands: [{ brand: 'Chromium', version: major }, { brand: 'Google Chrome', version: major }, { brand: 'Not?A_Brand', version: '99' }],
      fullVersion: `${major}.0.0.0`, platform: 'Windows', platformVersion: '15.0.0', architecture: 'x86', model: '', mobile: false,
    },
  }, sessionId);
}
// Meet asks whether the camera is allowed before it asks for it. Allowed is
// safe here: the page only ever gets Koli's tracks, and the browser was
// started with fake devices, so the real ones can't be reached at all.
await browser.send('Browser.grantPermissions', { origin: 'https://meet.google.com', permissions: ['videoCapture', 'audioCapture'] });
if (!process.env.KOLI_NO_CAMERA) await browser.send('Page.addScriptToEvaluateOnNewDocument', { source: inject }, sessionId);

let lastCpu = await browser.cpuSeconds();
let lastCpuAt = Date.now();

async function body(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
}

let mouse = { x: 420, y: 520 };   // where the pointer was last left

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const q = url.searchParams;
  let out;
  try {
    if (url.pathname === '/goto') {
      await browser.send('Page.navigate', { url: q.get('url') }, sessionId);
      out = { ok: true };
    } else if (url.pathname === '/shot') {
      out = { file: await browser.screenshot(sessionId, path.join(here, `shot-${name}-${q.get('name') || Date.now()}.png`)) };
    } else if (url.pathname === '/eval') {
      out = { value: await browser.evaluate(sessionId, await body(req)) };
    } else if (url.pathname === '/text') {
      await browser.send('Input.insertText', { text: await body(req) }, sessionId);
      out = { ok: true };
    } else if (url.pathname === '/type') {
      // Like a person: one real key press per letter, Shift for capitals, uneven pauses.
      for (const ch of await body(req)) {
        const upper = ch !== ch.toLowerCase();
        const code = /[a-z]/i.test(ch) ? `Key${ch.toUpperCase()}` : ch === ' ' ? 'Space' : '';
        const vk = ch.toUpperCase().charCodeAt(0);
        const modifiers = upper ? 8 : 0;
        if (upper) await browser.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Shift', code: 'ShiftLeft', windowsVirtualKeyCode: 16, modifiers: 8 }, sessionId);
        await browser.send('Input.dispatchKeyEvent', { type: 'keyDown', key: ch, code, text: ch, unmodifiedText: ch.toLowerCase(), windowsVirtualKeyCode: vk, modifiers }, sessionId);
        await sleep(40 + Math.random() * 60);
        await browser.send('Input.dispatchKeyEvent', { type: 'keyUp', key: ch, code, windowsVirtualKeyCode: vk, modifiers }, sessionId);
        if (upper) await browser.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Shift', code: 'ShiftLeft', windowsVirtualKeyCode: 16 }, sessionId);
        await sleep(90 + Math.random() * 160);
      }
      out = { ok: true };
    } else if (url.pathname === '/hclick') {
      // Like a person: the pointer travels to the spot in small uneven steps, rests, then clicks.
      const to = { x: Number(q.get('x')), y: Number(q.get('y')) };
      const from = mouse;
      const steps = 18 + Math.floor(Math.random() * 10);
      for (let i = 1; i <= steps; i++) {
        const t = i / steps;
        const ease = t * t * (3 - 2 * t);
        const x = from.x + (to.x - from.x) * ease + (Math.random() - 0.5) * 3;
        const y = from.y + (to.y - from.y) * ease + (Math.random() - 0.5) * 3;
        await browser.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y }, sessionId);
        await sleep(12 + Math.random() * 22);
      }
      await browser.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: to.x, y: to.y }, sessionId);
      await sleep(180 + Math.random() * 250);
      const point = { x: to.x, y: to.y, button: 'left', clickCount: 1 };
      await browser.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, buttons: 1 }, sessionId);
      await sleep(60 + Math.random() * 70);
      await browser.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point }, sessionId);
      mouse = to;
      out = { ok: true };
    } else if (url.pathname === '/key') {
      const key = KEYS[q.get('k')] || { key: q.get('k'), text: q.get('k') };
      await browser.send('Input.dispatchKeyEvent', { type: 'keyDown', ...key }, sessionId);
      await browser.send('Input.dispatchKeyEvent', { type: 'keyUp', ...key }, sessionId);
      out = { ok: true };
    } else if (url.pathname === '/click') {
      const point = { x: Number(q.get('x')), y: Number(q.get('y')), button: 'left', clickCount: 1 };
      await browser.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: point.x, y: point.y }, sessionId);
      await browser.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point }, sessionId);
      await browser.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point }, sessionId);
      out = { ok: true };
    } else if (url.pathname === '/board') {
      out = { draws: await browser.evaluate(sessionId, `window.__koli.board(${await body(req)})`) };
    } else if (url.pathname === '/stats') {
      const cpu = await browser.cpuSeconds();
      const now = Date.now();
      out = {
        seconds: +((now - lastCpuAt) / 1000).toFixed(1),
        cpuPercentOfOneCore: +(((cpu - lastCpu) / ((now - lastCpuAt) / 1000)) * 100).toFixed(1),
        memory: browser.memory(),
      };
      lastCpu = cpu;
      lastCpuAt = now;
    } else if (url.pathname === '/minimize' || url.pathname === '/restore') {
      const { windowId } = await browser.send('Browser.getWindowForTarget', { targetId });
      await browser.send('Browser.setWindowBounds', { windowId, bounds: { windowState: url.pathname === '/minimize' ? 'minimized' : 'normal' } });
      out = { ok: true };
    } else if (url.pathname === '/close') {
      res.end('{"ok":true}');
      await browser.close();
      server.close();
      fs.rmSync(path.join(here, `t2-${name}.port`), { force: true });
      process.exit(0);
    } else {
      out = { error: 'unknown command' };
    }
  } catch (e) {
    out = { error: String(e.message || e) };
  }
  res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(out));
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
fs.writeFileSync(path.join(here, `t2-${name}.port`), String(server.address().port));
console.log(`ready ${version.product} ${mode} port ${server.address().port} pid ${browser.child.pid}`);
