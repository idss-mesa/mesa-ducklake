#!/usr/bin/env bash
# backup-pg.sh — daily Postgres catalog backup for mesa-ducklake.
#
# Runs as the mesa-mcp service user (per the systemd timer). Dumps
# the mesa_ducklake database with pg_dump --format=custom, uploads
# the dump into iRODS, and prunes anything older than $RETENTION_DAYS.
#
# Why iRODS, not S3 or local disk: the rest of mesa-mcp's data lives
# in iRODS, including the Parquet snapshots this catalog indexes. A
# single durable store keeps recovery procedures coherent: an iget +
# pg_restore is all an operator needs to revive the catalog after a
# VM loss.
#
# Required environment:
#   PG_HOST            — Postgres host (default 127.0.0.1)
#   PG_PORT            — Postgres port (default 5432)
#   PG_DB              — Database name (default mesa_ducklake)
#   PG_USER            — Postgres role (default mesa)
#   PGPASSWORD_FILE    — path to a file holding the role's password
#                        (default /etc/mesa-mcp/secrets/postgres_password)
#   IRODS_BACKUP_DIR   — iRODS collection to upload into. Must already
#                        exist and be writable. Default
#                        /iplant/anvil/backup/mesa-ducklake/<hostname>
#   RETENTION_DAYS     — keep dumps newer than this many days; older
#                        get irm'd. Default 30. Set 0 to keep forever.
#
# Exit codes:
#   0  success (dump uploaded, optional retention pass succeeded)
#   1  pg_dump failed
#   2  iput failed
#   3  retention pruning produced an error (non-fatal — dump is safe)
#   4  preflight failed (missing password file, missing iCommands, …)

set -euo pipefail

PG_HOST="${PG_HOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
PG_DB="${PG_DB:-mesa_ducklake}"
PG_USER="${PG_USER:-mesa}"
PGPASSWORD_FILE="${PGPASSWORD_FILE:-/etc/mesa-mcp/secrets/postgres_password}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

HOSTNAME_SHORT="$(hostname -s)"
IRODS_BACKUP_DIR="${IRODS_BACKUP_DIR:-/iplant/anvil/backup/mesa-ducklake/${HOSTNAME_SHORT}}"

TODAY="$(date -u +%Y-%m-%d)"
DUMP_NAME="mesa-ducklake-${TODAY}.dump"
TMP_DIR="$(mktemp -d -t mesa-ducklake-backup.XXXXXX)"
DUMP_PATH="${TMP_DIR}/${DUMP_NAME}"

cleanup() {
    rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

log() {
    printf '%s mesa-ducklake-backup: %s\n' "$(date -u +%FT%TZ)" "$*" >&2
}

# ---------------------------------------------------------------------- preflight

if [[ ! -r "${PGPASSWORD_FILE}" ]]; then
    log "preflight: password file ${PGPASSWORD_FILE} not readable"
    exit 4
fi
if ! command -v pg_dump >/dev/null 2>&1; then
    log "preflight: pg_dump not on PATH"
    exit 4
fi
if ! command -v iput >/dev/null 2>&1; then
    log "preflight: iput not on PATH (iCommands required)"
    exit 4
fi
if ! command -v ils >/dev/null 2>&1; then
    log "preflight: ils not on PATH (iCommands required)"
    exit 4
fi

PGPASSWORD="$(<"${PGPASSWORD_FILE}")"
export PGPASSWORD

# ---------------------------------------------------------------------- dump

log "pg_dump ${PG_DB} -> ${DUMP_PATH}"
if ! pg_dump \
        --host="${PG_HOST}" \
        --port="${PG_PORT}" \
        --username="${PG_USER}" \
        --dbname="${PG_DB}" \
        --format=custom \
        --no-owner \
        --compress=9 \
        --file="${DUMP_PATH}"; then
    log "pg_dump failed"
    exit 1
fi
DUMP_SIZE="$(stat -c %s "${DUMP_PATH}" 2>/dev/null || stat -f %z "${DUMP_PATH}")"
log "pg_dump produced ${DUMP_SIZE} bytes"

# ---------------------------------------------------------------------- upload

# Ensure parent collection exists. ``imkdir -p`` is idempotent and
# tolerates pre-existing collections in modern iCommands.
if ! imkdir -p "${IRODS_BACKUP_DIR}" 2>/dev/null; then
    # Older iCommands may not have -p; fall back to per-segment imkdir.
    log "imkdir -p failed; trying segment-by-segment fallback"
    SEGMENTS=()
    REMAINING="${IRODS_BACKUP_DIR}"
    while [[ -n "${REMAINING}" && "${REMAINING}" != "/" ]]; do
        SEGMENTS=("${REMAINING}" "${SEGMENTS[@]}")
        REMAINING="${REMAINING%/*}"
    done
    for seg in "${SEGMENTS[@]}"; do
        ils "${seg}" >/dev/null 2>&1 || imkdir "${seg}" 2>/dev/null || true
    done
fi

IRODS_TARGET="${IRODS_BACKUP_DIR}/${DUMP_NAME}"
log "iput ${DUMP_PATH} -> ${IRODS_TARGET}"
if ! iput -f -K "${DUMP_PATH}" "${IRODS_TARGET}"; then
    log "iput failed"
    exit 2
fi
log "upload ok"

# ---------------------------------------------------------------------- retention

if [[ "${RETENTION_DAYS}" -le 0 ]]; then
    log "retention disabled (RETENTION_DAYS=${RETENTION_DAYS})"
    exit 0
fi

# Cutoff date in YYYY-MM-DD form. GNU date is fine on the deploy VM
# (Ubuntu 24.04). The pattern we keep is always ``mesa-ducklake-<date>.dump``.
CUTOFF="$(date -u -d "${RETENTION_DAYS} days ago" +%Y-%m-%d 2>/dev/null \
    || date -u -v"-${RETENTION_DAYS}d" +%Y-%m-%d)"
log "pruning dumps older than ${CUTOFF}"

PRUNE_ERROR=0
# ils output is one entry per line, prefixed with whitespace for files.
# Strip and filter to our dump pattern.
while IFS= read -r entry; do
    name="$(printf '%s' "${entry}" | sed -e 's/^[[:space:]]*//')"
    case "${name}" in
        mesa-ducklake-*.dump) ;;
        *) continue ;;
    esac
    # Extract the date portion: ``mesa-ducklake-YYYY-MM-DD.dump``.
    date_part="${name#mesa-ducklake-}"
    date_part="${date_part%.dump}"
    if [[ "${date_part}" < "${CUTOFF}" ]]; then
        log "irm ${IRODS_BACKUP_DIR}/${name}"
        if ! irm -f "${IRODS_BACKUP_DIR}/${name}"; then
            log "irm failed for ${name}"
            PRUNE_ERROR=1
        fi
    fi
done < <(ils "${IRODS_BACKUP_DIR}" 2>/dev/null | tail -n +2)

if [[ "${PRUNE_ERROR}" -ne 0 ]]; then
    # Dump landed; retention pass had trouble. Surface non-fatal so
    # the operator notices but doesn't lose the (successful) backup.
    exit 3
fi
exit 0
