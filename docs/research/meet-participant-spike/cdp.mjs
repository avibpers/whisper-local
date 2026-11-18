// cdp.mjs  (THROWAWAY SPIKE)
// Starts a browser with its own profile and drives it over the Chrome
// DevTools Protocol with nothing but Node's built-in WebSocket. Only the
// process started here is ever closed or killed.

import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';

export const BINARIES = {
  chrome: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  chromium: path.join(process.env.LOCALAPPDATA, 'ms-playwright', 'chromium-1248', 'chrome-win64', 'chrome.exe'),
  shell: path.join(process.env.LOCALAPPDATA, 'ms-playwright', 'chromium_headless_shell-1248', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe'),
  cft: path.join(path.dirname(new URL(import.meta.url).pathname.slice(1)), '..', 'koli-meet-browsers', 'cft', 'chrome-win64', 'chrome.exe'),
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// A free port chosen here: with --remote-debugging-port=0 Chromium marks the
// browser as automation-controlled (navigator.webdriver = true).
function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

export class Browser {
  constructor(child, ws, profile) {
    this.child = child;
    this.ws = ws;
    this.profile = profile;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = [];
    ws.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject, method } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(`${method}: ${msg.error.message}`));
        else resolve(msg.result);
      } else if (msg.method) {
        for (const fn of this.listeners) fn(msg);
      }
    });
  }

  send(method, params = {}, sessionId, timeoutMs = 30000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method}: no answer in ${timeoutMs} ms`));
      }, timeoutMs);
      this.pending.set(id, {
        method,
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }

  on(fn) { this.listeners.push(fn); }

  // The first page tab, attached in flat mode: { targetId, sessionId }.
  async page() {
    const { targetInfos } = await this.send('Target.getTargets');
    const target = targetInfos.find((t) => t.type === 'page');
    const { sessionId } = await this.send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
    await this.send('Page.enable', {}, sessionId);
    return { targetId: target.targetId, sessionId };
  }

  async evaluate(sessionId, expression, timeoutMs = 30000) {
    const result = await this.send('Runtime.evaluate',
      { expression, awaitPromise: true, returnByValue: true, userGesture: true }, sessionId, timeoutMs);
    if (result.exceptionDetails) {
      const d = result.exceptionDetails;
      throw new Error(`page error: ${(d.exception && d.exception.description) || d.text}`);
    }
    return result.result.value;
  }

  async screenshot(sessionId, file) {
    const { data } = await this.send('Page.captureScreenshot', { format: 'png' }, sessionId, 15000);
    fs.writeFileSync(file, Buffer.from(data, 'base64'));
    return file;
  }

  // CPU seconds used so far by every process of this browser.
  async cpuSeconds() {
    const { processInfo } = await this.send('SystemInfo.getProcessInfo');
    return processInfo.reduce((sum, p) => sum + (p.cpuTime || 0), 0);
  }

  // Memory of this browser's whole process tree, in MB (working set), and how many processes.
  memory() {
    // Every process of this browser, whatever its file is called (chrome.exe or the headless shell).
    const script = `$mine = @(Get-CimInstance Win32_Process -Filter "ProcessId=${this.child.pid} OR ParentProcessId=${this.child.pid}");`
      + `"$($mine.Count) $([math]::Round((($mine | Measure-Object WorkingSetSize -Sum).Sum)/1MB))"`;
    try {
      const out = execFileSync('powershell', ['-NoProfile', '-Command', script], { encoding: 'utf8' }).trim();
      const [count, mb] = out.split(' ').map(Number);
      return { processes: count, mb };
    } catch (e) {
      return { processes: 0, mb: 0, error: String(e.message || e) };
    }
  }

  async close() {
    try { await this.send('Browser.close', {}, undefined, 5000); } catch (e) { /* already gone */ }
    await sleep(1500);
    if (this.child.exitCode === null) {
      // Only the tree of the process started here, by its PID.
      try { execFileSync('taskkill', ['/PID', String(this.child.pid), '/T', '/F'], { stdio: 'ignore' }); } catch (e) { /* gone */ }
    }
  }
}

// mode: 'headless' | 'offscreen' | 'window'
export async function launch({ binary, profileName, mode = 'headless', extraArgs = [], size = '1280,800' }) {
  const profile = path.join(path.dirname(new URL(import.meta.url).pathname.slice(1)), '..', 'koli-meet-profiles', profileName);
  fs.mkdirSync(profile, { recursive: true });
  const portFile = path.join(profile, 'DevToolsActivePort');
  fs.rmSync(portFile, { force: true });
  const port = await freePort();
  const args = [
    `--user-data-dir=${profile}`,
    `--remote-debugging-port=${port}`,
    '--no-first-run', '--no-default-browser-check',
    '--mute-audio',                          // the app's loopback capture must not hear the call twice
    '--lang=en-US',
    '--use-fake-device-for-media-stream',    // safety net: the real webcam and microphone can never be opened
    `--window-size=${size}`,
    ...(mode === 'headless' ? ['--headless=new'] : []),
    ...(mode === 'offscreen' ? ['--window-position=-32000,-32000'] : []),
    ...extraArgs,
    'about:blank',
  ];
  const child = spawn(BINARIES[binary], args, { stdio: 'ignore' });
  // With a port named on the command line Chrome writes no DevToolsActivePort
  // file (measured on 157), so the address comes from /json/version.
  let wsUrl = null;
  for (let i = 0; i < 100 && !wsUrl; i++) {
    await sleep(150);
    try {
      const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      wsUrl = version.webSocketDebuggerUrl;
    } catch (e) { /* not up yet */ }
  }
  if (!wsUrl) {
    try { execFileSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' }); } catch (e) { /* gone */ }
    throw new Error('the browser did not open its debugging port');
  }
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', () => reject(new Error('could not connect to the browser')), { once: true });
  });
  return new Browser(child, ws, profile);
}

export { sleep };
