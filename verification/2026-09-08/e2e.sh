#!/usr/bin/env bash
# End-to-end suite against production with every standalone service suspended.
# Retries each probe: this container's egress proxy intermittently drops CONNECTs,
# which looks identical to an outage if a single attempt is trusted.
S=/tmp/claude-0/-home-user/bcc90d1b-32be-5867-9c98-6a6f49d12757/scratchpad
pass=0; fail=0

probe() {  # probe <expected> <label> <url> [curl args...]
  local want=$1 label=$2 url=$3; shift 3
  local n=0 code=000
  until [ "$code" != "000" ] || [ $n -ge 6 ]; do
    rm -f "$S/e2e.body"
    code=$(curl -s -o "$S/e2e.body" -w '%{http_code}' --max-time 25 "$@" "$url" 2>/dev/null)
    code=${code:-000}
    [ "$code" = "000" ] && { n=$((n+1)); sleep 8; }
  done
  local body; body=$(head -c 58 "$S/e2e.body" 2>/dev/null | tr '\n' ' ')
  if [ "$code" = "$want" ]; then pass=$((pass+1)); printf "  PASS  %-46s %s  %s\n" "$label" "$code" "$body"
  else fail=$((fail+1)); printf "  FAIL  %-46s got %s want %s  %s\n" "$label" "$code" "$want" "$body"; fi
}

echo "--- gateway itself"
probe 200 "gateway health"                 https://backend-gateway-zyu0.onrender.com/__gateway/health
probe 404 "unknown host on the service"    https://backend-gateway-zyu0.onrender.com/no-such-project

echo "--- Heard, on the product's own API domain"
probe 200 "api.heard.cc /healthz"          https://api.heard.cc/healthz
probe 200 "api.heard.cc /docs"             https://api.heard.cc/docs
probe 200 "api.heard.cc /openapi.json"     https://api.heard.cc/openapi.json
probe 404 "api.heard.cc login unknown user" https://api.heard.cc/auth/token -X POST -d "username=nobody@example.invalid&password=wrong-pw-1"
probe 200 "api.heard.erdemgunseli.com"     https://api.heard.erdemgunseli.com/healthz
probe 200 "path alias /heard/healthz"      https://backend-gateway-zyu0.onrender.com/heard/healthz

echo "--- In-Sight"
probe 200 "api.insight.erdemgunseli.com /"  https://api.insight.erdemgunseli.com/
probe 200 "api.insight… /docs"              https://api.insight.erdemgunseli.com/docs
probe 404 "insight login unknown user"      https://api.insight.erdemgunseli.com/auth/token -X POST -d "username=nobody@example.invalid&password=wrong-pw-1"
probe 200 "path alias /insight/"            https://backend-gateway-zyu0.onrender.com/insight/
probe 200 "legacy alias /in-sight/"         https://backend-gateway-zyu0.onrender.com/in-sight/

echo "--- SEO Rise"
probe 200 "api.seorise.erdemgunseli.com /"  https://api.seorise.erdemgunseli.com/
probe 200 "api.seorise… /docs"              https://api.seorise.erdemgunseli.com/docs
probe 404 "seorise login unknown user"      https://api.seorise.erdemgunseli.com/auth/token -X POST -d "username=nobody@example.invalid&password=wrong-pw-1"
probe 200 "path alias /seorise/"            https://backend-gateway-zyu0.onrender.com/seorise/
probe 200 "legacy alias /seo-rise/"         https://backend-gateway-zyu0.onrender.com/seo-rise/

echo "--- the frontends that depend on all this"
probe 200 "heard.cc loads"                  https://heard.cc/
probe 200 "seorise.vercel.app loads"        https://seorise.vercel.app/

echo
echo "  $pass passed, $fail failed"
exit $((fail > 0))
