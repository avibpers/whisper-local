(() => {
  const out = [];
  for (const tile of document.querySelectorAll('[data-participant-id]')) {
    const name = (tile.querySelector('span.notranslate') || {}).innerText || '';
    const video = [...tile.querySelectorAll('video')].find((v) => v.getBoundingClientRect().width > 0);
    const t = tile.getBoundingClientRect();
    const row = { name, tile: `${Math.round(t.width)}x${Math.round(t.height)}` };
    if (video) {
      const r = video.getBoundingClientRect();
      const cs = getComputedStyle(video);
      row.video = `${video.videoWidth}x${video.videoHeight}`;
      row.box = `${Math.round(r.width)}x${Math.round(r.height)}`;
      row.fit = cs.objectFit;
    }
    out.push(row);
  }
  return JSON.stringify({ window: `${innerWidth}x${innerHeight}`, tiles: out });
})()
