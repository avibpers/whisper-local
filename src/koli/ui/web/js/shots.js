// shots.js
// Screenshots in a meeting, shared by the panel and the Meetings screen:
// the turns and the screenshots ordered by time, a thumbnail that fetches
// its PNG once (as a data URL, the way the bridge serves it), and the
// in-page lightbox that shows one full size (no new window, no alert).

import { html, useEffect, useState } from '../vendor/preact-htm.module.js';
import { call } from './api.js';

// Turns in time order with each screenshot before the first turn that
// starts after it (the same rule as rows_by_time in meeting/store.py).
export function rowsByTime(turns, screenshots) {
  const shots = [...(screenshots || [])].sort((a, b) => a.at - b.at);
  const ordered = [...(turns || [])].sort((a, b) => a.start - b.start);
  const rows = [];
  let i = 0;
  for (const t of ordered) {
    while (i < shots.length && shots[i].at <= t.start) rows.push({ kind: 'shot', ...shots[i++] });
    rows.push({ kind: 'turn', ...t });
  }
  for (; i < shots.length; i++) rows.push({ kind: 'shot', ...shots[i] });
  return rows;
}

// One fetch per image per page: the data URL never changes.
const images = new Map();
function imageUrl(meetingId, image) {
  const key = `${meetingId}/${image}`;
  if (!images.has(key)) {
    images.set(key, call('meeting_image', meetingId, image).then((d) => d.url).catch(() => { images.delete(key); return null; }));
  }
  return images.get(key);
}

export function useShotImage(meetingId, image) {
  const [url, setUrl] = useState(null);
  useEffect(() => { let live = true; imageUrl(meetingId, image).then((u) => live && setUrl(u)); return () => { live = false; }; }, [meetingId, image]);
  return url;
}

// A small image that opens the lightbox; `width` in px.
export function Thumb({ meetingId, shot, width = 160, onOpen }) {
  const url = useShotImage(meetingId, shot.image);
  const style = `width:${width}px;max-width:100%;aspect-ratio:16/10;border-radius:6px;border:1px solid var(--line, rgba(128,128,128,.4));background:rgba(128,128,128,.15);object-fit:cover;display:block;cursor:zoom-in`;
  if (!url) return html`<span style=${style} aria-label="Screenshot loading"></span>`;
  return html`<img src=${url} alt=${shot.caption || 'Screenshot'} title="Open" style=${style}
    onClick=${(e) => { e.stopPropagation(); onOpen && onOpen(shot); }} />`;
}

// The full image over the page; click outside or Esc closes it.
export function Lightbox({ meetingId, shot, onClose }) {
  const url = useShotImage(meetingId, shot.image);
  useEffect(() => {
    const key = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [onClose]);
  return html`<div role="dialog" aria-label="Screenshot" onClick=${onClose}
    style="position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.72);display:grid;place-items:center;padding:24px;cursor:zoom-out">
    ${url && html`<img src=${url} alt=${shot.caption || 'Screenshot'} style="max-width:94vw;max-height:86vh;border-radius:8px;box-shadow:0 12px 40px rgba(0,0,0,.5)" onClick=${(e) => e.stopPropagation()} />`}
    <div style="position:absolute;left:0;right:0;bottom:12px;text-align:center;color:#fff;font-size:13px;text-shadow:0 1px 3px #000">${shot.caption || ''}</div>
  </div>`;
}
