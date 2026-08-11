#!/usr/bin/env bash
# Smoke test against a running `wrangler dev` (default http://localhost:8787).
#
#   Terminal 1:  wrangler d1 execute avogadro-plugin-downloads --local --file=./schema.sql
#                wrangler dev
#   Terminal 2:  ./smoke-test.sh
set -euo pipefail

BASE="${1:-http://localhost:8787}"
UA="Avogadro/2.0 PackageManager"
SHA="12a58630dde5b7184dfafd97e67e0022c49f7a09"

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

echo "==> health"
curl -fsS "$BASE/health" >/dev/null && pass "responds"

echo "==> redirect for a known plugin"
loc=$(curl -sS -o /dev/null -D - -H "User-Agent: $UA" \
  "$BASE/dl/generators/$SHA.zip" | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}')
[ "$loc" = "https://github.com/OpenChemistry/avogenerators/archive/$SHA.zip" ] \
  && pass "$loc" || fail "unexpected Location: ${loc:-none}"

echo "==> full-name alias resolves to the same repo"
loc2=$(curl -sS -o /dev/null -D - -H "User-Agent: $UA" \
  "$BASE/dl/avogadro-generators/$SHA.zip" | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}')
[ "$loc2" = "$loc" ] && pass "alias matches" || fail "alias gave ${loc2:-none}"

echo "==> repo-name aliases resolve (repo name != plugin name)"
# avogadro-xtb lives in matterhorn103/avo_xtb, avogadro-generators in avogenerators.
for alias in xtb avo_xtb avo-xtb avogadro-xtb; do
  loc=$(curl -sS -o /dev/null -D - -H "User-Agent: $UA" \
    "$BASE/dl/$alias/HEAD.zip" | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}')
  [ "$loc" = "https://github.com/matterhorn103/avo_xtb/archive/HEAD.zip" ] \
    && pass "$alias" || fail "$alias gave ${loc:-none}"
done
loc=$(curl -sS -o /dev/null -D - -H "User-Agent: $UA" \
  "$BASE/dl/avogenerators/HEAD.zip" | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}')
[ "$loc" = "https://github.com/OpenChemistry/avogenerators/archive/HEAD.zip" ] \
  && pass "avogenerators" || fail "avogenerators gave ${loc:-none}"

echo "==> aliases all count into one bucket"
base=$(curl -sS "$BASE/stats/xtb" | grep -o '"total": *[0-9]*' | grep -o '[0-9]*')
[ "$base" = "4" ] && pass "4 alias hits landed under 'xtb'" \
  || fail "expected 4 under 'xtb', got $base"
curl -sS "$BASE/stats" | grep -q '"avo-xtb"\|"avo_xtb"\|"avogadro-xtb"' \
  && fail "an alias leaked its own bucket" || pass "no alias buckets"

echo "==> unknown plugin is rejected"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "User-Agent: $UA" "$BASE/dl/not-a-plugin/$SHA.zip")
[ "$code" = "404" ] && pass "404" || fail "expected 404, got $code"

echo "==> traversal ref is rejected"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "User-Agent: $UA" "$BASE/dl/generators/..%2f..%2fevil.zip")
[ "$code" = "400" ] || [ "$code" = "404" ] && pass "$code" || fail "expected 400/404, got $code"

echo "==> counting"
# Baseline, so the test is repeatable against a database that already has rows.
before=$(curl -sS "$BASE/stats/generators" | grep -o '"total": *[0-9]*' | grep -o '[0-9]*')
for _ in 1 2 3; do
  curl -sS -o /dev/null -H "User-Agent: $UA" "$BASE/dl/generators/$SHA.zip"
done
curl -sS -o /dev/null -H "User-Agent: $UA" "$BASE/dl/avogadro-generators/$SHA.zip"  # alias
curl -sS -o /dev/null -H "User-Agent: Mozilla/5.0" "$BASE/dl/generators/$SHA.zip"   # not an install
curl -sS -o /dev/null -I -H "User-Agent: $UA" "$BASE/dl/generators/$SHA.zip"        # HEAD, must not count
sleep 1

after=$(curl -sS "$BASE/stats/generators" | grep -o '"total": *[0-9]*' | grep -o '[0-9]*')
delta=$(( after - before ))
# 3 loops + 1 alias. The Mozilla request is 'other', the HEAD counts nowhere.
[ "$delta" = "4" ] && pass "counted 4 installs (alias folded in, HEAD and browser excluded)" \
  || fail "expected delta 4, got $delta (before=$before after=$after)"

echo "==> alias does not create a second bucket"
curl -sS "$BASE/stats" | grep -q '"avogadro-generators"' \
  && fail "alias leaked a separate 'avogadro-generators' key" \
  || pass "single canonical key"

echo "==> non-install traffic is still recorded separately"
all=$(curl -sS "$BASE/stats/generators?client=all" | grep -o '"total": *[0-9]*' | grep -o '[0-9]*')
[ "$all" -gt "$after" ] && pass "client=all ($all) exceeds installs ($after)" \
  || fail "expected client=all to exceed the install count"

echo
echo "All checks passed."
