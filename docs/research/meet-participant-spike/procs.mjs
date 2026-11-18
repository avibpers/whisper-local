// procs.mjs <browser pid> <seconds>  (THROWAWAY SPIKE)
// Which of a test browser's processes use the CPU: page, graphics, network, audio.
import { execFileSync } from 'node:child_process';
const [pid, seconds = '30'] = process.argv.slice(2);
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
const snap = async () => Object.fromEntries((await send('SystemInfo.getProcessInfo')).processInfo.map((p) => [p.id, p]));
const a = await snap();
await new Promise((r) => setTimeout(r, seconds * 1000));
const b = await snap();
const rows = Object.values(b).map((p) => ({ type: p.type, percent: +(((p.cpuTime - (a[p.id] ? a[p.id].cpuTime : 0)) / seconds) * 100).toFixed(1) })).filter((r) => r.percent >= 0.3).sort((x, y) => y.percent - x.percent);
console.log(JSON.stringify(rows));
const gpu = await send('SystemInfo.getInfo');
console.log(JSON.stringify({ gpu: gpu.gpu && gpu.gpu.devices && gpu.gpu.devices.map((d) => d.deviceString), videoDecoding: gpu.gpu && gpu.gpu.videoDecoding && gpu.gpu.videoDecoding.length, features: gpu.gpu && gpu.gpu.featureStatus && { gpu_compositing: gpu.gpu.featureStatus.gpu_compositing, video_decode: gpu.gpu.featureStatus.video_decode, video_encode: gpu.gpu.featureStatus.video_encode, webgl: gpu.gpu.featureStatus.webgl } }));
ws.close();
