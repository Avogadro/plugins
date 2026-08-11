/**
 * Download counter for the Avogadro plugin index.
 *
 * Routes:
 *   GET /dl/<plugin>/<ref>.zip   count the request, 302 to the GitHub archive
 *   GET /stats                   per-plugin totals as JSON
 *   GET /stats/<plugin>          one plugin, plus its daily series
 *   GET /health                  liveness probe
 *
 * The plugin -> repository mapping comes from the published index, so this
 * Worker can only ever redirect to a repository that is already in the index.
 */

const DEFAULT_INDEX_URL = 'https://avogadro.cc/plugins2.json';
const INDEX_TTL_MS = 10 * 60 * 1000;
const DEFAULT_WINDOW_DAYS = 30;

// Refs are commit SHAs or release tags. Deliberately strict: no slashes, so a
// crafted ref cannot walk out of the /archive/ path into another repository.
const REF_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const PLUGIN_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const DL_RE = /^\/dl\/([A-Za-z0-9._-]+)\/([A-Za-z0-9._-]+)\.zip$/;
const BOT_RE = /bot|crawl|spider|slurp|curl|wget|preview|monitor|scan/i;

// Module-global, so it survives between requests on a warm isolate.
let indexCache = { at: 0, map: null };

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.length > 1 ? url.pathname.replace(/\/+$/, '') : '/';

    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return json({ error: 'method not allowed' }, 405);
    }

    try {
      if (path === '/health') {
        return json({ ok: true });
      }
      if (path === '/stats') {
        return await handleStats(url, env, null, ctx);
      }
      if (path.startsWith('/stats/')) {
        return await handleStats(url, env, path.slice('/stats/'.length), ctx);
      }
      if (path.startsWith('/dl/')) {
        return await handleDownload(request, path, env, ctx);
      }
    } catch (err) {
      console.error('unhandled error', err);
      return json({ error: 'internal error' }, 500);
    }

    return json({ error: 'not found' }, 404);
  },
};

// --- downloads -------------------------------------------------------------

async function handleDownload(request, path, env, ctx) {
  const match = DL_RE.exec(path);
  if (!match) {
    return json({ error: 'expected /dl/<plugin>/<ref>.zip' }, 400);
  }

  const [, plugin, ref] = match;
  if (!PLUGIN_RE.test(plugin) || !validRef(ref)) {
    return json({ error: 'invalid plugin or ref' }, 400);
  }

  const repos = await pluginRepoMap(env, ctx);
  const entry = repos.get(normalizeName(plugin));
  if (!entry) {
    return json({ error: `unknown plugin '${plugin}'` }, 404);
  }

  const target = `https://github.com/${entry.slug}/archive/${ref}.zip`;

  // HEAD requests are probes, not installs.
  if (request.method === 'GET') {
    const client = classifyClient(request.headers.get('user-agent'));
    // Count under the canonical short name, so that requests for `generators`
    // and `avogadro-generators` land in one bucket rather than two.
    ctx.waitUntil(record(env, entry.key, ref, client));
  }

  // 302 rather than 301, and no-store, so that no intermediary caches the hop
  // and silently swallows future counts.
  return new Response(null, {
    status: 302,
    headers: { Location: target, 'Cache-Control': 'no-store' },
  });
}

/**
 * The D1 handle, under either binding name. `wrangler d1 create` suggests a
 * binding named after the database, so both spellings are in circulation and
 * a mismatch is otherwise a silent 500.
 */
function database(env) {
  return env.DB ?? env.avogadro_plugin_downloads ?? null;
}

async function record(env, plugin, ref, client) {
  const db = database(env);
  if (!db) {
    console.error('no D1 binding; check `binding` in wrangler.toml');
    return;
  }
  const day = new Date().toISOString().slice(0, 10);
  try {
    await db.prepare(
      `INSERT INTO downloads (plugin, ref, day, client, count)
       VALUES (?1, ?2, ?3, ?4, 1)
       ON CONFLICT (plugin, ref, day, client)
       DO UPDATE SET count = count + 1`
    )
      .bind(plugin, ref, day, client)
      .run();
  } catch (err) {
    // A stats failure must never cost somebody a plugin install.
    console.error('record failed', err);
  }
}

function validRef(ref) {
  return REF_RE.test(ref) && !ref.includes('..');
}

function classifyClient(userAgent) {
  if (!userAgent) return 'other';
  if (userAgent.startsWith('Avogadro/')) return 'avogadro';
  if (BOT_RE.test(userAgent)) return 'bot';
  return 'other';
}

// --- stats -----------------------------------------------------------------

async function handleStats(url, env, plugin, ctx) {
  const days = clampInt(url.searchParams.get('days'), DEFAULT_WINDOW_DAYS, 1, 3650);
  const client = url.searchParams.get('client') || 'avogadro';
  if (!['avogadro', 'bot', 'other', 'all'].includes(client)) {
    return json({ error: `invalid client '${client}'` }, 400);
  }
  const cutoff = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);

  const db = database(env);
  if (!db) {
    // Say which knob is wrong rather than a bare 500. Downloads keep working
    // when this is misconfigured, so stats are where it first shows up.
    return json({ error: 'no D1 binding configured' }, 503);
  }

  const body = {
    generated: new Date().toISOString(),
    window_days: days,
    client,
  };

  if (plugin) {
    if (!PLUGIN_RE.test(plugin)) {
      return json({ error: 'invalid plugin' }, 400);
    }
    // Accept any alias here too, so `/stats/avo_xtb` and `/stats/xtb` agree.
    // An unrecognised name falls through unchanged rather than 404ing, so that
    // rows for a plugin since removed from the index stay queryable.
    const repos = await pluginRepoMap(env, ctx);
    plugin = repos.get(normalizeName(plugin))?.key ?? plugin;

    const totals = await db.prepare(
      `SELECT SUM(count) AS total,
              SUM(CASE WHEN day >= ?2 THEN count ELSE 0 END) AS recent
       FROM downloads
       WHERE plugin = ?1 AND (?3 = 'all' OR client = ?3)`
    )
      .bind(plugin, cutoff, client)
      .first();

    const daily = await db.prepare(
      `SELECT day, SUM(count) AS count
       FROM downloads
       WHERE plugin = ?1 AND day >= ?2 AND (?3 = 'all' OR client = ?3)
       GROUP BY day
       ORDER BY day`
    )
      .bind(plugin, cutoff, client)
      .all();

    body.plugin = plugin;
    body.total = totals?.total ?? 0;
    body.recent = totals?.recent ?? 0;
    body.daily = daily.results ?? [];
    return json(body, 200, { 'Cache-Control': 'public, max-age=300' });
  }

  const rows = await db.prepare(
    `SELECT plugin,
            SUM(count) AS total,
            SUM(CASE WHEN day >= ?1 THEN count ELSE 0 END) AS recent
     FROM downloads
     WHERE (?2 = 'all' OR client = ?2)
     GROUP BY plugin
     ORDER BY plugin`
  )
    .bind(cutoff, client)
    .all();

  body.plugins = {};
  for (const row of rows.results ?? []) {
    body.plugins[row.plugin] = { total: row.total, recent: row.recent };
  }
  return json(body, 200, { 'Cache-Control': 'public, max-age=300' });
}

function clampInt(raw, fallback, min, max) {
  const n = Number.parseInt(raw ?? '', 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

// --- plugin index ----------------------------------------------------------

/**
 * Map of accepted plugin name -> {key, slug}, where `key` is the canonical
 * short name used for counting and `slug` is `owner/repo`. Both the short and
 * the full `avogadro-` prefixed name are accepted as lookup keys.
 */
async function pluginRepoMap(env, ctx) {
  const now = Date.now();
  if (indexCache.map && now - indexCache.at < INDEX_TTL_MS) {
    return indexCache.map;
  }

  const indexUrl = env.INDEX_URL || DEFAULT_INDEX_URL;
  let entries = null;
  let body = null;

  // One retry: most blips are a single request, and a cold isolate has no
  // in-memory copy to fall back on.
  for (let attempt = 0; attempt < 2 && entries === null; attempt++) {
    try {
      if (attempt > 0) await sleep(150);
      const res = await fetch(indexUrl, {
        cf: { cacheTtl: 600, cacheEverything: true },
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
      body = await res.text();
      entries = JSON.parse(body);
    } catch (err) {
      console.error(`index fetch failed (attempt ${attempt + 1})`, err);
    }
  }

  if (entries === null) {
    // Serve a stale allowlist rather than break installs while avogadro.cc is
    // having a bad day. In-memory first, then the last good copy in D1, which
    // is all a cold isolate has.
    if (indexCache.map) return indexCache.map;
    const stored = await loadStoredIndex(env);
    if (stored) {
      entries = stored;
      body = null; // nothing new to persist
    } else {
      throw new Error('plugin index unavailable and no cached copy');
    }
  } else if (body !== null) {
    ctx?.waitUntil?.(storeIndex(env, body));
  }

  const valid = (Array.isArray(entries) ? entries : [])
    .map((entry) => ({ name: entry?.name, slug: repoSlug(entry?.git?.repo) }))
    .filter((e) => typeof e.name === 'string' && e.slug);

  const map = new Map();

  // First pass: keys derived from the plugin name, which the index generator
  // guarantees is `avogadro-<something>`. These are authoritative.
  for (const { name, slug } of valid) {
    const key = stripPrefix(normalizeName(name));
    map.set(normalizeName(name), { key, slug });
    map.set(key, { key, slug });
  }

  // Second pass: keys derived from the repository name, which does not have to
  // match the plugin name -- `avogadro-xtb` lives in `avo_xtb`, and
  // `avogadro-generators` in `avogenerators`. A repo-derived alias must never
  // shadow a name-derived key, hence the separate pass: index order should not
  // decide who wins.
  for (const { name, slug } of valid) {
    const key = stripPrefix(normalizeName(name));
    const repo = normalizeName(slug.split('/')[1]);
    for (const alias of [repo, stripPrefix(repo)]) {
      if (!map.has(alias)) map.set(alias, { key, slug });
    }
  }

  indexCache = { at: now, map };
  return map;
}

/** Persist the last good index so a cold isolate has something to fall back on. */
async function storeIndex(env, body) {
  const db = database(env);
  if (!db) return;
  try {
    await db
      .prepare(
        `INSERT INTO index_cache (id, body, fetched_at) VALUES (1, ?1, ?2)
         ON CONFLICT (id) DO UPDATE SET body = ?1, fetched_at = ?2`
      )
      .bind(body, new Date().toISOString())
      .run();
  } catch (err) {
    console.error('index_cache write failed', err);
  }
}

async function loadStoredIndex(env) {
  const db = database(env);
  if (!db) return null;
  try {
    const row = await db.prepare(`SELECT body FROM index_cache WHERE id = 1`).first();
    if (!row?.body) return null;
    console.error('serving plugin index from D1 fallback');
    return JSON.parse(row.body);
  } catch (err) {
    console.error('index_cache read failed', err);
    return null;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** PEP 503-style: lowercase, and `-`/`_`/`.` runs collapsed to a single `-`. */
function normalizeName(name) {
  return name.toLowerCase().replace(/[-_.]+/g, '-');
}

/**
 * Drop a leading `avogadro-` or `avo-` from an already-normalized name.
 *
 * Only separated prefixes are stripped. A bare `avo` is left alone: it would
 * turn `avogenerators` into `generators` correctly but also `avocado` into
 * `cado`, and the name-derived pass already supplies the short form anyway.
 */
function stripPrefix(normalized) {
  for (const prefix of ['avogadro-', 'avo-']) {
    if (normalized.startsWith(prefix) && normalized.length > prefix.length) {
      return normalized.slice(prefix.length);
    }
  }
  return normalized;
}

function repoSlug(repoUrl) {
  if (typeof repoUrl !== 'string') return null;
  const m = /^https:\/\/github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/.exec(
    repoUrl
  );
  return m ? `${m[1]}/${m[2]}` : null;
}

// --- helpers ---------------------------------------------------------------

function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
      ...headers,
    },
  });
}
