#!/usr/bin/env bash
# Prove the production SQLite write path end to end on SEO Rise: create an account,
# see the row exist, authenticate against it, then delete it and see it gone. Every
# step is observed through the public API, so nothing depends on reading a job log.
# Leaves no residue - the account it creates is the account it deletes.
set -u
B=https://api.seorise.erdemgunseli.com
EMAIL="gateway-sqlite-check-$(date -u +%Y%m%d%H%M%S)@erdemgunseli.com"
PW="Tmp-$(head -c 9 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')9!"
pass=0; fail=0
chk(){ local want=$1 label=$2 got=$3 extra=${4:-}
  if [ "$got" = "$want" ]; then pass=$((pass+1)); printf "  PASS  %-46s %s %s\n" "$label" "$got" "$extra"
  else fail=$((fail+1)); printf "  FAIL  %-46s got %s want %s %s\n" "$label" "$got" "$want" "$extra"; fi; }

echo "--- 1. the account does not exist yet"
c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 -X POST "$B/auth/token" -d "username=$EMAIL&password=$PW")
chk 404 "login before registering" "$c"

echo "--- 2. write: register (an INSERT into /data/seorise.db)"
c=$(curl -s -o r1.json -w '%{http_code}' --max-time 30 -X POST "$B/user/" -H 'Content-Type: application/json' \
      -d "{\"name\":\"Gateway Check\",\"email\":\"$EMAIL\",\"password\":\"$PW\"}")
chk 201 "register" "$c" "$(head -c 40 r1.json | tr '\n' ' ')"

echo "--- 3. the row is really there: unique constraint holds, and the password verifies"
c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST "$B/user/" -H 'Content-Type: application/json' \
      -d "{\"name\":\"Gateway Check\",\"email\":\"$EMAIL\",\"password\":\"$PW\"}")
chk 409 "duplicate registration rejected" "$c"
c=$(curl -s -o tok.json -w '%{http_code}' --max-time 30 -X POST "$B/auth/token" -d "username=$EMAIL&password=$PW")
chk 200 "login with the right password" "$c"
c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 -X POST "$B/auth/token" -d "username=$EMAIL&password=wrong-$PW")
chk 401 "login with the wrong password" "$c"

TOKEN=$(python3 -c "import json;print(json.load(open('tok.json')).get('access_token',''))" 2>/dev/null)
echo "--- 4. delete: remove the row again, so this leaves nothing behind"
c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X DELETE "$B/user/" -H "Authorization: Bearer $TOKEN")
chk 204 "delete the account" "$c"
c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 -X POST "$B/auth/token" -d "username=$EMAIL&password=$PW")
chk 404 "login after deleting" "$c"
rm -f r1.json tok.json
echo; echo "  $pass passed, $fail failed"; exit $((fail>0))
