#!/usr/bin/env sh
# Verify the ArmyEye -> FACE_DETECTOR webhook integration, from INSIDE the VMS container,
# exactly as the application would perform it. Mode-agnostic: run it before and after moving
# ArmyEye to its own server; a PASS means the same thing in both cases.
#
#   sh scripts/check-face-integration.sh
#
# Checks, in the order a real delivery would fail:
#   1. the name resolves at all                (DNS / extra_hosts / Docker alias)
#   2. TCP reaches the port                    (routing, firewall)
#   3. TLS verifies against the mounted CA     (cert trust + hostname/SAN match)
#   4. the webhook route exists and enforces auth (401 for a deliberately bad token)
#
# It sends NO detection data - only an empty body with an invalid token, so a PASS proves the
# path works without writing anything into FACE.
set -eu
cd "$(dirname "$0")/.."

docker inspect VMS >/dev/null 2>&1 || { echo "VMS container is not running"; exit 1; }
BASE=$(docker exec VMS sh -c 'tr "\0" "\n" < /proc/1/environ | sed -n "s/^WEBHOOK_BASE_URL=//p"')
CA=$(docker exec VMS sh -c 'tr "\0" "\n" < /proc/1/environ | sed -n "s/^REQUESTS_CA_BUNDLE=//p"')
[ -n "$BASE" ] || { echo "WEBHOOK_BASE_URL is not set on the app process - webhooks are disabled"; exit 1; }

echo "target : $BASE"
echo "CA     : ${CA:-<none - TLS will use the public bundle only>}"
docker exec -i -e B="$BASE" -e CA="${CA:-}" VMS python - <<'PY'
import os, socket, ssl, sys, urllib.error, urllib.parse, urllib.request

base = os.environ["B"]; ca = os.environ.get("CA") or None
u = urllib.parse.urlparse(base)
host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
fails = []

# 1. name resolution - tells you at a glance which mode you are in
try:
    ip = socket.gethostbyname(host)
    where = "Docker network alias (same host)" if ip.startswith(("172.", "10.88.")) else "real address"
    print(f"  PASS  resolves      {host} -> {ip}   [{where}]")
except Exception as e:
    print(f"  FAIL  resolves      {host}: {e}"); fails.append("dns")
    print("        same host  -> is the container on the webhook_integration network?")
    print("        other host -> set FACE_HOST_IP and add compose.remote-face.yaml, or add a DNS record")
    sys.exit(1)

# 2. TCP reachability - separates routing/firewall problems from TLS problems
try:
    socket.create_connection((host, port), timeout=8).close()
    print(f"  PASS  reachable     tcp/{port}")
except Exception as e:
    print(f"  FAIL  reachable     tcp/{port}: {e}"); fails.append("tcp")
    print("        check the route between the hosts and that FACE nginx is published")
    sys.exit(1)

# 3. TLS trust + hostname match
if u.scheme == "https":
    try:
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        with socket.create_connection((host, port), timeout=8) as s:
            with ctx.wrap_socket(s, server_hostname=host) as t:
                cert = t.getpeercert()
        san = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
        print(f"  PASS  TLS verified  cert SAN {san} covers {host}")
    except ssl.SSLCertVerificationError as e:
        print(f"  FAIL  TLS verified  {e.verify_message}"); fails.append("tls")
        print("        hostname mismatch -> WEBHOOK_BASE_URL must use a name in the cert SAN")
        print("        unknown CA        -> is FACE's internal CA mounted (FACE_INTERNAL_CA_FILE)?")
        sys.exit(1)
    except Exception as e:
        print(f"  FAIL  TLS verified  {e}"); fails.append("tls"); sys.exit(1)
else:
    print(f"  WARN  TLS           {base} is plain http - detections and the bearer token "
          f"would cross the network in clear text")

# 4. route exists and auth is enforced
req = urllib.request.Request(f"{base}/webhook/armyeye-integration-check", data=b"{}", method="POST",
                             headers={"Content-Type": "application/json",
                                      "Authorization": "Bearer deliberately-invalid"})
try:
    ctx = ssl.create_default_context(cafile=ca) if (ca and u.scheme == "https") else None
    urllib.request.urlopen(req, context=ctx, timeout=15)
    print("  FAIL  auth          an invalid token was ACCEPTED"); fails.append("auth")
except urllib.error.HTTPError as e:
    if e.code in (401, 403):
        print(f"  PASS  auth          route reached, invalid token rejected -> HTTP {e.code}")
    elif e.code == 404:
        print("  FAIL  route         HTTP 404 - the /webhook/ path is not served here"); fails.append("route")
    else:
        print(f"  WARN  route         unexpected HTTP {e.code}")
except Exception as e:
    print(f"  FAIL  request       {type(e).__name__}: {e}"); fails.append("req")

print()
print("INTEGRATION: " + ("PASS - the delivery path works end to end" if not fails
                         else f"FAIL ({', '.join(fails)})"))
sys.exit(1 if fails else 0)
PY
