/* Orion's Belt — shared client utilities */

// Auth token helper (cookie-based auth is primary; this is a no-op stub
// kept for compatibility with first_run.html's startApp() call)
function setAuthToken(token) {
  // Token is set as an httponly cookie by the server; nothing to do client-side
}

// HTML-escape a string for safe insertion into innerHTML.
function escHtml(s) {
  return (s == null ? '' : String(s))
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// fetch() wrapper returning parsed JSON, with an optional short-lived cache.
//   cachedFetch(url)                       → always fetches
//   cachedFetch(url, opts)                 → always fetches with opts
//   cachedFetch(url, opts, ttlMs)          → serves a cached body for ttlMs
// Only GET requests are cached. Throws on a non-2xx response.
const _fetchCache = new Map();
async function cachedFetch(url, opts = {}, ttlMs = 0) {
  const method = (opts.method || 'GET').toUpperCase();
  const cacheable = ttlMs > 0 && method === 'GET';
  const now = Date.now();

  if (cacheable) {
    const hit = _fetchCache.get(url);
    if (hit && (now - hit.at) < ttlMs) {
      return hit.data;
    }
  }

  const res = await fetch(url, opts);
  if (!res.ok) {
    throw new Error(`${method} ${url} → ${res.status}`);
  }
  const data = await res.json();

  if (cacheable) {
    _fetchCache.set(url, { at: now, data });
  }
  return data;
}

// Invalidate a cachedFetch entry (call after a mutating write to a cached URL).
function invalidateFetchCache(url) {
  if (url) _fetchCache.delete(url);
  else _fetchCache.clear();
}

// Build a compaction summary card element for the chat transcript.
// Expects: { messages_compacted, summary, timestamp }
function createCompactionCard(c) {
  const div = document.createElement('div');
  div.className = 'flex items-center gap-3 my-3 px-3 py-2 rounded-lg border border-border-subtle bg-bg-raised/50 text-xs text-text-muted';
  const n = c && c.messages_compacted != null ? c.messages_compacted : 0;
  const summary = (c && c.summary) ? c.summary : `Context compacted (${n} messages archived)`;
  let ts = '';
  if (c && c.timestamp) {
    const d = new Date(c.timestamp);
    if (!isNaN(d)) ts = d.toLocaleString();
  }
  div.innerHTML = `
    <i data-lucide="archive" class="w-3.5 h-3.5 flex-shrink-0 text-accent"></i>
    <span class="flex-1">${escHtml(summary)}</span>
    ${ts ? `<span class="opacity-60 flex-shrink-0">${escHtml(ts)}</span>` : ''}`;
  return div;
}
