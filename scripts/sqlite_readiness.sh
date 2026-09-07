#!/usr/bin/env bash
# Does each hosted backend actually WORK on SQLite, not merely boot on it?
# Boots the real gateway (Caddy + supervisord + the three project venvs) against
# throwaway SQLite files and exercises the write path that matters: register a real
# account, then log in with the right password and the wrong one. Anything a
# Postgres-only assumption would break - enum columns, JSON columns, timestamp
# defaults, unique constraints, cascade FKs - is on that path.
S=/tmp/claude-0/-home-user/bcc90d1b-32be-5867-9c98-6a6f49d12757/scratchpad
B=http://127.0.0.1:18080
pass=0; fail=0

check() {  # check <label> <expected-code> <actual-code> [body]
  if [ "$2" = "$3" ]; then pass=$((pass+1)); printf "  PASS  %-44s %s  %s\n" "$1" "$3" "$(echo "$4" | head -c 60)"
  else fail=$((fail+1)); printf "  FAIL  %-44s got %s want %s  %s\n" "$1" "$3" "$2" "$(echo "$4" | head -c 90)"; fi
}

post_json() { curl -s -o "$S/rb" -w '%{http_code}' --max-time 20 -H "Host: $1" -H 'Content-Type: application/json' -d "$3" "$B$2"; }
post_form() { curl -s -o "$S/rb" -w '%{http_code}' --max-time 20 -H "Host: $1" -d "$3" "$B$2"; }

run_product() {  # run_product <label> <host> <register-payload>
  local label=$1 host=$2 payload=$3
  local email; email=$(echo "$payload" | grep -o '"email": *"[^"]*"' | cut -d'"' -f4)
  local pw;    pw=$(echo "$payload" | grep -o '"password": *"[^"]*"' | cut -d'"' -f4)
  echo "--- $label on SQLite"
  local code; code=$(post_json "$host" /user/ "$payload"); check "$label register" 201 "$code" "$(cat "$S/rb")"
  code=$(post_json "$host" /user/ "$payload");             check "$label duplicate email rejected" 409 "$code" "$(cat "$S/rb")"
  code=$(post_form "$host" /auth/token "username=$email&password=$pw")
  check "$label login (correct password)" 200 "$code" "$(cat "$S/rb")"
  code=$(post_form "$host" /auth/token "username=$email&password=definitely-wrong-1")
  check "$label login (wrong password)" 401 "$code" "$(cat "$S/rb")"
}

run_product "Heard"    api.heard.cc                  '{"name": "Sqlite Probe", "email": "sqlite.probe@example.com", "password": "Str0ng-Probe-Pw-42"}'
run_product "In-Sight" api.insight.erdemgunseli.com  '{"name": "Sqlite Probe", "email": "sqlite.probe@example.com", "password": "Str0ng-Probe-Pw-42"}'
run_product "SEO Rise" api.seorise.erdemgunseli.com  '{"name": "Sqlite Probe", "email": "sqlite.probe@example.com", "password": "Str0ng-Probe-Pw-42"}'

echo
echo "  $pass passed, $fail failed"
