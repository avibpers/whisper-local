// brand.js
// Koli's mark: a turquoise circle holding a "K" that breathes out a small
// sound wave. Used by the app window's top bar, Home and the edge bubble, so
// the logo is drawn in one place.

import { html } from '../vendor/preact-htm.module.js';

export const NAME = 'Koli';

// `size` is the circle's diameter in px; the stroke thickens as it shrinks
// so the K stays legible down to tray-icon sizes.
export function KoliMark({ size = 36, wave = true }) {
  const glyph = Math.round(size * 0.6);
  const stroke = size >= 60 ? 4.2 : size >= 32 ? 5 : 6;
  return html`<span class="brand-mark" style=${`width:${size}px;height:${size}px`} aria-hidden="true">
    <svg viewBox="0 0 48 48" width=${glyph} height=${glyph} fill="none" stroke="currentColor" stroke-width=${stroke}
      stroke-linecap="round" stroke-linejoin="round">
      <path d="M16 12v24"></path><path d="M32 13L22 24l10 11"></path>
      ${wave && html`<path d="M37.5 19.5c2 2.6 2 6.4 0 9"></path>`}
    </svg></span>`;
}
