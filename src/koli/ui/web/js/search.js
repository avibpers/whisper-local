// search.js
// Matching for the Settings search: every word you type must appear somewhere
// in the text a setting is known by (its name, help, group, section and
// internal name), in any order and any case. Pure and import-free, so the
// tests can run it with Node.

// The words of a query, lowercased; an empty query has none.
export function searchTerms(query) {
  return String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
}

// True when every term appears in the text (always true with no terms).
export function matchesTerms(text, terms) {
  const hay = String(text || '').toLowerCase();
  return terms.every((t) => hay.includes(t));
}

// Everything a schema setting can be found by.
export function settingHaystack(setting, sectionTitle = '') {
  return [setting.label, setting.help, setting.path, setting.group, sectionTitle].filter(Boolean).join(' ');
}
