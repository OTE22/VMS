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
SRC="${ARMYEYE_CONFIG_ENCRYPTION_KEY_SOURCE:-/run/secrets/armyeye_config_key}"
DST_DIR="${ARMYEYE_CONFIG_KEY_STAGE_DIR:-/run/armyeye}"
if [ -f "$SRC" ]; then
  umask 077
  mkdir -p "$DST_DIR" 2>/dev/null || true
  chmod 0700 "$DST_DIR" 2>/dev/null || true
  cp "$SRC" "$DST_DIR/config.key"
  chmod 0400 "$DST_DIR/config.key"
  export ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE="$DST_DIR/config.key"
elif [ -n "${ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE:-}" ] && [ ! -f "$ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE" ]; then
  echo "[entrypoint] WARNING: no configuration encryption key mounted at $SRC - starting without a key (fail-safe)" >&2
fi
exec "$@"
