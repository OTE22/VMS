# Moving ArmyEye to its own server

ArmyEye currently shares a host with FACE_DETECTOR. This is the procedure for separating
them. It was written while the two were still co-located, so the configuration was built to
make the move a **config change, not a re-architecture**.

## What actually changes — and what must not

Exactly one thing is host-dependent: **how `face-detector.internal` resolves.**

| | Same host (today) | Separate hosts |
|---|---|---|
| Name resolution | `webhook_integration` Docker alias → `172.19.0.2` | real DNS record, or `extra_hosts` via `compose.remote-face.yaml` |
| `WEBHOOK_BASE_URL` | `https://face-detector.internal` | **unchanged** |
| TLS trust | FACE's internal CA, mounted from a file | **unchanged** |
| Bearer token | from FACE's `webhook_api_keys` | **unchanged value**, provisioned on the new host |
| Certificate | SAN covers `face-detector.internal` | **unchanged** |

**Never change `WEBHOOK_BASE_URL` to an IP or to `http://`.** An IP fails hostname
verification against FACE's certificate; `http://` would put detection payloads *and the
bearer token* on the wire in clear text. The name is what makes the move a one-line change.

## Before you move

- [ ] `sh scripts/check-face-integration.sh` passes on the old host (baseline)
- [ ] `bash scripts/smoke-deploy.sh` passes on the old host
- [ ] The new host has: NVIDIA driver + Container Toolkit, Docker, and a **private network
      path to FACE** (LAN/VLAN/VPN — detection data must never traverse the public internet)
- [ ] Note FACE nginx's real address, reachable from the new host

## ⚠️ The one thing that is easy to get wrong

**`secrets/armyeye_config_key` MUST move with the database.**

Publisher and telemetry credentials are stored encrypted (`{"v":1,"key_id":…,"ct":…}`).
The key lives only in that file — never in PostgreSQL, by design. Restore the database
without the key and **every encrypted credential becomes permanently unreadable**; the
application will refuse to serve them (fail-safe) and you will have to re-enter each one by
hand. There is no recovery path, because there is deliberately no copy of the key anywhere
else.

Current key id: run `cut -d: -f1 secrets/armyeye_config_key`.

Verify after restore that the key ids match what the data expects:
```bash
docker exec VMS-db psql -U armeye -d armeye -tAc \
  "select distinct config->>'key_id' from publishers where config ? 'key_id'
   union select distinct value->>'key_id' from node_settings where value ? 'key_id'"
```

## Procedure

### 1. Quiesce the old host
```bash
cd ~/Desktop/VMS
docker compose stop vms          # stop writes; leave the database up for the dump
```

### 2. Take the data
```bash
# database (schema + rows)
docker exec VMS-db pg_dump -U armeye -d armeye -Fc > /tmp/armeye-$(date +%F).dump

# artifact bytes: models, custom engines, media, thumbnails
tar -C InferenceNode -czf /tmp/armeye-artifacts-$(date +%F).tgz data

# secrets - the config key above all
tar -czf /tmp/armeye-secrets-$(date +%F).tgz secrets .env
```
Move all three over a private channel. Treat the secrets archive as a credential.

### 3. Prepare the new host
```bash
git clone <repo> ~/VMS && cd ~/VMS
tar -xzf /tmp/armeye-secrets-*.tgz          # restores secrets/ and .env
tar -C InferenceNode -xzf /tmp/armeye-artifacts-*.tgz
chmod 700 secrets && chmod 600 secrets/armyeye_config_key .env
```

Edit `.env` for the new mode:
```ini
COMPOSE_FILE=compose.yaml:compose.prod.yaml:compose.gpu.yaml:compose.remote-face.yaml
FACE_HOST_IP=<FACE nginx's real address>
# WEBHOOK_BASE_URL stays exactly as it was
```
If you have real internal DNS, add an `A` record for `face-detector.internal` instead and
omit both the overlay and `FACE_HOST_IP`.

### 4. Build and start
```bash
./docker-start.sh --build        # validates GPU, runtime, network and compose first
```
The database starts empty; alembic migrates to head. Then restore:
```bash
docker compose stop vms
docker exec -i VMS-db pg_restore -U armeye -d armeye --clean --if-exists < /tmp/armeye-*.dump
docker compose start vms
```

### 5. Verify — do not skip
```bash
sh scripts/check-face-integration.sh   # must PASS, and now show a real address, not 172.x
bash scripts/smoke-deploy.sh           # must be 22/22
```
Then check the startup log shows `[VERIFY] registry reconciliation: healthy`. If artifacts
were missed, this reports them as `MISSING`/`available_missing` rather than failing silently:
```bash
docker logs VMS 2>&1 | grep -E "VERIFY|MIGRATE"
```
Finally confirm encrypted credentials still decrypt — open Publishers in the UI and check
each destination is usable (secrets stay redacted as `***`; that is correct).

### 6. Decommission
Only after the new host has run correctly for a full day: stop the old stack, keep the dump
and the secrets archive as a cold backup, and remove the `webhook_integration` network from
the old machine if nothing else uses it.

## Rollback

Nothing on the old host is destroyed by this procedure. To go back, start the old stack
(`docker compose up -d`) and stop the new one. The FACE side needs no change either way —
its `face-detector.internal` alias serves both.

## What the move does not carry over

- **mDNS `.local` names** are host-specific; set up the new host's name there.
- **The `webhook_integration` Docker network** is meaningless across hosts. The container
  still joins it (harmlessly, empty) so both modes share one compose base.
- **Node discovery on UDP 8888** is LAN-scoped and will only see peers on the new segment.
