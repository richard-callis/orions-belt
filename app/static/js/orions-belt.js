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

// ── Markdown rendering ───────────────────────────────────────────────────────
// Mirrors ORION web's chat markdown: marked (GFM) → highlight.js code fences →
// DOMPurify sanitize. ```mermaid fences render as native diagrams. Returns a
// sanitized HTML string safe for innerHTML. Falls back to escaped text if the
// libs failed to load.
let _markedConfigured = false;
let _mermaidInitialized = false;

function _configureMarked() {
  if (_markedConfigured || typeof marked === 'undefined') return;
  try {
    // Custom code-block renderer: a ```mermaid fence becomes a <div
    // class="mermaid"> holding the raw diagram source, which mermaid.run()
    // (called from renderMarkdownInto) replaces with an SVG. Everything else
    // renders as a normal <pre><code class="language-X"> for hljs to pick up.
    const renderer = new marked.Renderer();
    renderer.code = function (token) {
      const isTokenObj = token && typeof token === 'object';
      const text = isTokenObj ? (token.text ?? '') : String(token ?? '');
      const rawLang = isTokenObj ? token.lang : arguments[1];
      const lang = (rawLang || '').trim().split(/\s+/)[0].toLowerCase();
      if (lang === 'mermaid') {
        return `<div class="mermaid">${escHtml(text)}</div>`;
      }
      const cls = lang ? ` class="language-${escHtml(lang)}"` : '';
      return `<pre><code${cls}>${escHtml(text)}</code></pre>`;
    };
    marked.setOptions({ gfm: true, breaks: true, renderer });
  } catch (e) {}
  _markedConfigured = true;
}

function renderMarkdown(text) {
  if (!text) return '';
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    // Libs unavailable — degrade to escaped text with newlines preserved.
    return escHtml(text).replace(/\n/g, '<br>');
  }
  _configureMarked();
  let raw;
  try {
    raw = marked.parse(String(text));
  } catch (e) {
    return escHtml(text).replace(/\n/g, '<br>');
  }
  // Sanitize the marked output; keep the safe subset of markdown-produced tags.
  return DOMPurify.sanitize(raw, {
    ADD_ATTR: ['target', 'rel'],
    FORBID_TAGS: ['style', 'form', 'input', 'iframe', 'script'],
    FORBID_ATTR: ['onerror', 'onload', 'onclick', 'style'],
  });
}

// Render a markdown string into an element: sanitize, harden links,
// syntax-highlight code fences with highlight.js, and render mermaid diagrams.
function renderMarkdownInto(el, text) {
  if (!el) return;
  el.innerHTML = renderMarkdown(text);
  el.querySelectorAll('a[href]').forEach(a => {
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
  });
  if (typeof hljs !== 'undefined') {
    el.querySelectorAll('pre code').forEach(block => {
      try { hljs.highlightElement(block); } catch (e) {}
    });
  }
  if (typeof mermaid !== 'undefined') {
    const nodes = el.querySelectorAll('.mermaid');
    if (nodes.length) {
      try {
        if (!_mermaidInitialized) {
          mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict' });
          _mermaidInitialized = true;
        }
        // Diagram source may still be arriving mid-stream (incomplete syntax);
        // mermaid renders an inline error for a malformed diagram and the next
        // re-render self-heals once the full source has streamed in.
        const result = mermaid.run({ nodes: Array.from(nodes), suppressErrors: true });
        if (result && typeof result.catch === 'function') result.catch(() => {});
      } catch (e) {}
    }
  }
}

// Highlight @mentions in plain (human) message text — @everyone yellow, other
// @mentions accent — mirroring ORION web's MessageContent. Escapes everything
// else and preserves newlines.
function highlightMentions(text) {
  if (!text) return '';
  return String(text)
    .split(/(@[\w-]+)/g)
    .map(part => {
      if (/^@[\w-]+$/.test(part)) {
        const cls = part.toLowerCase() === '@everyone'
          ? 'text-status-warning font-semibold'
          : 'text-accent font-medium';
        return `<span class="${cls}">${escHtml(part)}</span>`;
      }
      return escHtml(part);
    })
    .join('')
    .replace(/\n/g, '<br>');
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
