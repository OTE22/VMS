#!/bin/sh
# ArmyEye container entrypoint.
#
# Configuration-encryption key staging: the key arrives as a MOUNTED file
# (compose secret / bind mount; never baked into the image, never in PostgreSQL). On some
# hosts (Docker Desktop for Windows/macOS) a bind-mounted file cannot carry a restrictive
# mode - it shows up as 0777 root - and config_secrets refuses a group/world-readable key
# (fail-safe). So we stage a PRIVATE 0400 copy in a container-only tmpfs owned by the app
# user and point the application at that copy. Nothing is ever written back to the host.
#   ARMYEYE_CONFIG_ENCRYPTION_KEY_SOURCE  mounted key (default /run/secrets/armyeye_config_key)
#   ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE    what the application reads (set here to the copy)
# If no key is mounted, the application starts WITHOUT a key: encrypted values stay
# unreadable and un-storable (fail-safe), exactly as designed - it never generates one.
set -e
umask 077
SRC="${ARMYEYE_CONFIG_ENCRYPTION_KEY_SOURCE:-/run/secrets/armyeye_config_key}"
DST_DIR="${ARMYEYE_CONFIG_KEY_STAGE_DIR:-/run/armyeye}"
mkdir -p "$DST_DIR" 2>/dev/null || true
chmod 0700 "$DST_DIR" 2>/dev/null || true
if [ -f "$SRC" ]; then
  cp "$SRC" "$DST_DIR/config.key"
  chmod 0400 "$DST_DIR/config.key"
  export ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE="$DST_DIR/config.key"
elif [ -n "${ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE:-}" ] && [ ! -f "$ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE" ]; then
  echo "[entrypoint] WARNING: no configuration encryption key mounted at $SRC - starting without a key (fail-safe)" >&2
fi

# Private-CA trust for outbound HTTPS (webhooks to FACE_DETECTOR, whose certificate is
# signed by its own internal CA). The mounted CA is APPENDED to the public bundle that
# ships with `certifi`, so verification of FACE succeeds while public downloads (e.g.
# Ultralytics weights) keep working. Pointing REQUESTS_CA_BUNDLE at the private CA alone
# would silently break every public HTTPS call. Verification is never disabled.
#   ARMYEYE_CA_SOURCE  mounted CA certificate (default /run/secrets/face_internal_ca)
CA_SRC="${ARMYEYE_CA_SOURCE:-/run/secrets/face_internal_ca}"
if [ -f "$CA_SRC" ]; then
  BUNDLE="$DST_DIR/ca-bundle.pem"
  PUB="$(python -c 'import certifi,sys; sys.stdout.write(certifi.where())' 2>/dev/null || true)"
  if [ -n "$PUB" ] && [ -f "$PUB" ] && { cat "$PUB"; echo; cat "$CA_SRC"; } > "$BUNDLE" 2>/dev/null; then
    chmod 0400 "$BUNDLE"
    export REQUESTS_CA_BUNDLE="$BUNDLE" SSL_CERT_FILE="$BUNDLE"
  else
    echo "[entrypoint] WARNING: could not build CA bundle from $CA_SRC - private-CA HTTPS targets will fail verification" >&2
  fi
fi
exec "$@"
