-- Download counters for the Avogadro plugin index.
--
-- One row per (plugin, ref, day, client). Daily granularity keeps the table
-- small -- ~30 plugins x ~365 days is a few tens of thousands of rows a year --
-- while still supporting both lifetime totals and rolling windows.
--
-- No IP addresses, no user agent strings, no per-request rows: `client` is a
-- coarse bucket only.

CREATE TABLE IF NOT EXISTS downloads (
  plugin TEXT NOT NULL,     -- short plugin name, e.g. "aimnet2"
  ref    TEXT NOT NULL,     -- commit SHA or release tag that was fetched
  day    TEXT NOT NULL,     -- UTC date, YYYY-MM-DD
  client TEXT NOT NULL,     -- 'avogadro' | 'bot' | 'other'
  count  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (plugin, ref, day, client)
) WITHOUT ROWID;

-- Rolling-window queries filter on day first.
CREATE INDEX IF NOT EXISTS idx_downloads_day ON downloads (day);

-- Last known good copy of the plugin index.
--
-- A cold isolate has no in-memory copy, so without this a transient failure
-- fetching plugins2.json turns into a failed plugin install. Single row.
CREATE TABLE IF NOT EXISTS index_cache (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  body       TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
