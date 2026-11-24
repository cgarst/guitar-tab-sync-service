#!/bin/sh
# Smoke test against a running service: tests/smoke.sh [base-url] [song-id]
# Use song 23063 (or any real ID) against the real container; the fake upstream
# in tests/fake_upstream accepts any ID and fails on 666.
set -eu
BASE=${1:-http://localhost:8080}; SONG=${2:-23063}
fail() { echo "FAIL: $*" >&2; exit 1; }

curl -fsS "$BASE/healthz" >/dev/null || fail healthz
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -d song=abc "$BASE/api/jobs")
[ "$code" = 400 ] || fail "bad song should be 400, got $code"

resp=$(curl -fsS -X POST -d "song=$SONG" "$BASE/api/jobs")
id=$(printf '%s' "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "job $id"
for _ in $(seq 1 240); do
  st=$(curl -fsS "$BASE/api/job?id=$id" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [ "$st" = done ] || [ "$st" = failed ] && break
  sleep 3
done
curl -fsS "$BASE/api/job?id=$id"; echo
[ "$st" = done ] || fail "job ended $st"
curl -fsS -D - -o /tmp/gpsync-smoke.gp "$BASE/api/job/file?id=$id" | grep -i content-disposition
[ -s /tmp/gpsync-smoke.gp ] || fail "empty file"
curl -fsS "$BASE/job?id=$id" | grep -q 'majestictab://open?url=' || fail "open link missing on page"
echo "PASS"
