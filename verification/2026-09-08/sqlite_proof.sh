#!/usr/bin/env bash
# Proof that the SQLite databases really carry the migrated accounts: a login attempt
# with a REAL migrated address and a wrong password must be rejected as a bad password
# (401), while an address that never existed must be rejected as unknown (404). The two
# answers differ only because the app read the row - so a 401 here is the row existing.
# Emails are read from a 0600 file and never printed.
S=/tmp/claude-0/-home-user/bcc90d1b-32be-5867-9c98-6a6f49d12757/scratchpad
mapfile -t E < /tmp/real_emails.txt
pass=0; fail=0
probe() { local want=$1 label=$2 url=$3; shift 3
  local n=0 code=000
  until [ "$code" != "000" ] || [ $n -ge 6 ]; do
    code=$(curl -s -o "$S/e2e.body" -w '%{http_code}' --max-time 25 "$@" "$url" 2>/dev/null); code=${code:-000}
    [ "$code" = "000" ] && { n=$((n+1)); sleep 8; }
  done
  if [ "$code" = "$want" ]; then pass=$((pass+1)); printf "  PASS  %-52s %s\n" "$label" "$code"
  else fail=$((fail+1)); printf "  FAIL  %-52s got %s want %s  %s\n" "$label" "$code" "$want" "$(head -c 70 "$S/e2e.body" | tr '\n' ' ')"; fi
}
echo "--- the migrated row is there (401 = found, wrong password)"
probe 401 "heard   real migrated account"   https://api.heard.cc/auth/token            -X POST -d "username=${E[0]}&password=definitely-not-the-password-1"
probe 401 "insight real migrated account"   https://api.insight.erdemgunseli.com/auth/token -X POST -d "username=${E[1]}&password=definitely-not-the-password-1"
probe 401 "seorise real migrated account"   https://api.seorise.erdemgunseli.com/auth/token  -X POST -d "username=${E[2]}&password=definitely-not-the-password-1"
echo "--- and an address that never existed is still unknown (404)"
probe 404 "heard   unknown account"         https://api.heard.cc/auth/token            -X POST -d "username=nobody@example.invalid&password=definitely-not-the-password-1"
probe 404 "insight unknown account"         https://api.insight.erdemgunseli.com/auth/token -X POST -d "username=nobody@example.invalid&password=definitely-not-the-password-1"
probe 404 "seorise unknown account"         https://api.seorise.erdemgunseli.com/auth/token  -X POST -d "username=nobody@example.invalid&password=definitely-not-the-password-1"
echo; echo "  $pass passed, $fail failed"; exit $((fail>0))
