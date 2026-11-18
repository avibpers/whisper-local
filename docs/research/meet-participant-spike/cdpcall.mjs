// cdpcall.mjs <browser pid> <left> <top> <width> <height>  (THROWAWAY SPIKE)
// Moves and resizes the window of a test browser that is already running.
import { execFileSync } from 'node:child_process';
const [pid, left, top, width, height] = process.argv.slice(2);
const cmd = execFileSync('powershell', ['-NoProfile', '-Command', `(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}").CommandLine`], { encoding: 'utf8' });
const port = cmd.match(/--remote-debugging-port=(\d+)/)[1];
const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
const ws = new WebSocket(version.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let id = 0;
const send = (method, params = {}) => new Promise((resolve) => {
  const mine = ++id;
  const on = (e) => { const m = JSON.parse(e.data); if (m.id === mine) { ws.removeEventListener('message', on); resolve(m.result || m.error); } };
  ws.addEventListener('message', on);
  ws.send(JSON.stringify({ id: mine, method, params }));
});
const { targetInfos } = await send('Target.getTargets');
const page = targetInfos.find((t) => t.type === 'page' && /meet\.google/.test(t.url));
const { windowId } = await send('Browser.getWindowForTarget', { targetId: page.targetId });
await send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'normal' } });
console.log(JSON.stringify(await send('Browser.setWindowBounds', { windowId, bounds: { left: +left, top: +top, width: +width, height: +height } })));
console.log(JSON.stringify((await send('Browser.getWindowForTarget', { targetId: page.targetId })).bounds));
ws.close();
