# Plugin download counter

A Cloudflare Worker that counts plugin installs. GitHub does not report download
counts for auto-generated source archives (`/archive/<ref>.zip`), and the traffic
API needs push access to each repository, so the only way to measure installs is
to count them ourselves.

The Worker sits in front of the GitHub archive URL as a redirect hop:

```
Avogadro -> https://plugins.avogadro.cc/dl/aimnet2/<ref>.zip   (counted, 302)
         -> https://github.com/ghutchis/avogadro-aimnet2/archive/<ref>.zip
         -> https://codeload.github.com/...                    (GitHub's own hop)
```

The archive bytes are untouched, so the `sha256` already recorded in
`plugins2.json` still validates. Avogadro's package manager follows the hop with
no client change: `packagemanagerdialog.cpp` sets `NoLessSafeRedirectPolicy` and
handles 301/302/307/308 explicitly.

## Routes

| Route | Purpose |
|-------|---------|
| `GET /dl/<plugin>/<ref>.zip` | Count the request, 302 to the GitHub archive |
| `GET /stats` | Per-plugin `total` and `recent` counts |
| `GET /stats/<plugin>` | One plugin, plus a daily series |
| `GET /health` | Liveness probe |

`/stats` accepts `?days=` (default 180) and `?client=avogadro\|bot\|other\|all`
(default `avogadro`).

### Plugin names

The index generator guarantees every plugin `name` is `avogadro-<something>`,
but the **repository** name need not match: `avogadro-xtb` lives in `avo_xtb`,
`avogadro-ibo` in `avo_ibo`, `avogadro-generators` in `avogenerators`.

So a plugin is accepted under its plugin name, its repo name, and either with a
leading `avogadro-` or `avo-` removed. Names are compared PEP 503-style, with
`-`, `_` and `.` treated alike, so `avo_xtb` and `avo-xtb` are the same thing.
All of these reach the same plugin and count into the same bucket:

```
/dl/xtb/<ref>.zip   /dl/avo_xtb/...   /dl/avo-xtb/...   /dl/avogadro-xtb/...
```

Counts are always recorded under the short canonical name (`xtb`), never under
whichever alias the request happened to use.

Only *separated* prefixes are stripped. A bare `avo` is left alone -- it would
turn `avogenerators` into `generators` correctly, but also `avocado` into
`cado`. `avogenerators` still resolves, because the plugin name supplies the
short form independently.

Repo-derived aliases are registered in a second pass and never overwrite a
name-derived key, so index order cannot decide which plugin claims a contested
alias. Across the 28 repositories currently in `repositories.toml` this yields
59 aliases with no ambiguity; `scripts/` has no checker for this yet, so it is
worth re-running that audit if a plugin is ever added whose repo name collides
with another plugin's short name.

## What is stored

One row per `(plugin, ref, day, client)` with a counter. No IP addresses, no
user agent strings, no per-request rows. `client` is a coarse bucket derived
from the user agent: `avogadro` for the package manager, `bot` for obvious
crawlers, `other` for everything else. Only `avogadro` counts as an install.

## Safety properties

- The plugin -> repository map is built from the published index, so the Worker
  can only redirect to a repository already listed there. It is not an open
  redirect.
- Refs are restricted to `[A-Za-z0-9._-]` with no slashes and no `..`, so a
  crafted ref cannot escape the `/archive/` path.
- A D1 write failure is logged and swallowed. Stats must never cost an install.
- If the index is unreachable the fetch is retried once, then falls back to the
  in-memory copy, then to the last good copy in the `index_cache` table. A cold
  isolate has no in-memory copy, so without the D1 fallback a single hiccup
  fetching `plugins2.json` would turn into a failed install.
- The D1 binding is read as either `DB` or `avogadro_plugin_downloads`, since
  `wrangler d1 create` suggests a binding named after the database. A mismatch
  would otherwise leave downloads working while `/stats` returned a bare 500.

## Local testing

```sh
npx wrangler d1 execute avogadro-plugin-downloads --local --file=./schema.sql
npx wrangler dev
./smoke-test.sh          # in another terminal
```

`smoke-test.sh` checks the redirect target, the name alias, rejection of unknown
plugins and traversal refs, and that counting excludes HEAD requests and browser
traffic. It is repeatable -- it baselines the counter first.

To exercise the outage path, point the Worker at an index that is not there and
confirm downloads still resolve from the D1 fallback:

```sh
npx wrangler dev --var INDEX_URL:http://127.0.0.1:9/plugins2.json
```

To inspect the local database directly:

```sh
npx wrangler d1 execute avogadro-plugin-downloads --local \
  --command "SELECT * FROM downloads ORDER BY day DESC LIMIT 20;"
```

## Deploying

`avogadro.cc` is already on Cloudflare nameservers, so the zone is in the
account and no DNS record has to be created by hand.

**1. Authenticate.** Opens a browser for OAuth:

```sh
npx wrangler login
npx wrangler whoami          # confirm the right account, if you have several
```

**2. Create the database.** `wrangler.toml` already carries the `database_id`
of the deployed instance, so this step is only needed when standing up a fresh
one, in which case put the printed id in `wrangler.toml`:

```sh
npx wrangler d1 create avogadro-plugin-downloads
```

**3. Create the table** in the remote database. `--local` and `--remote` are
separate databases; the local one used for testing is not touched by deploys:

```sh
npx wrangler d1 execute avogadro-plugin-downloads --remote --file=./schema.sql
```

**4. Deploy.** This prints a `*.workers.dev` URL:

```sh
npx wrangler deploy
```

**5. Test against the deployed Worker** before wiring up the domain:

```sh
./smoke-test.sh https://avogadro-plugin-downloads.<subdomain>.workers.dev
```

Note that this writes real rows into the production database. To clear them:

```sh
npx wrangler d1 execute avogadro-plugin-downloads --remote \
  --command "DELETE FROM downloads;"
```

The `[[routes]]` block is already active in `wrangler.toml`, so step 4 serves
on `plugins.avogadro.cc` directly. Cloudflare created the DNS record and its
certificate when the route was first deployed.

That also means the `workers.dev` URL does **not** serve: wrangler disables the
subdomain whenever a route is configured, unless `workers_dev = true` is set.
To get a URL for testing that is not the live domain, comment the `[[routes]]`
block out for that deploy, or set `workers_dev = true` alongside it. Step 5 then
has somewhere to point that is not production.

### Rollback

`npx wrangler deployments list` and `npx wrangler rollback [id]`. Deleting the
route is a config change plus a redeploy; the D1 data survives both.

### Cost

Well inside the free tier at plugin-index volume -- one Worker request and one
row written per install. Check the current numbers at
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/) and
[D1 limits](https://developers.cloudflare.com/d1/platform/limits/) before
assuming headroom, since the free-tier daily caps do change.

### What is safe to commit

`wrangler.toml` is committed deliberately. `database_id` and `database_name`
are identifiers, not credentials: reaching that database still requires an API
token scoped to the Cloudflare account. The same goes for `account_id` if it is
ever added here.

Never commit:

- Cloudflare API tokens. CI deploys read `CLOUDFLARE_API_TOKEN` from a GitHub
  Actions secret.
- `.dev.vars`, which holds local secret vars for `wrangler dev`.

Values set with `wrangler secret put` live in Cloudflare and never touch the
repo, and the OAuth token from `wrangler login` is stored in `~/.config/` rather
than in the project.

One consequence of committing `database_id`: a fork that runs `wrangler deploy`
targets a database id its account cannot reach, so the deploy fails with a
permissions error. That is a mildly confusing first run for a contributor, not
a way into this database.

### Docs

- [Workers: get started](https://developers.cloudflare.com/workers/get-started/guide/)
- [D1: get started](https://developers.cloudflare.com/d1/get-started/)
- [Custom domains for Workers](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)
- [Wrangler commands](https://developers.cloudflare.com/workers/wrangler/commands/)

## Clients must send a User-Agent

Cloudflare answers the default `Python-urllib/...` agent with a 403. Anything
talking to this Worker needs to identify itself: `generate_index.py` sends
`avogadro-plugin-index/...`, and Avogadro itself sends
`Avogadro/2.0 PackageManager`, which is also what marks a request as an install
rather than incidental traffic.

## Notes

- Redirecting straight to `codeload.github.com/<owner>/<repo>/zip/<ref>` would
  save one hop, but the `github.com/archive/<ref>.zip` form is what the index
  uses today and is correct for both commit SHAs and tags. Not worth the risk.
- `handleRedirect()` in `packagemanagerdialog.cpp` follows redirects with no hop
  limit. That is fine for this two-hop chain, but a misconfigured redirect loop
  would spin forever. Worth a counter there independently of this Worker.
