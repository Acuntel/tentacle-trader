#!/usr/bin/env bash
# Pull the live learned brain OFF the Railway volume to a local timestamped tarball.
# The bot persists state on the volume + daily on-volume backups, but that's ONE location — this
# gets an off-volume copy so a volume loss can never erase the learning.
#   Usage:  bash scripts/backup_state.sh [dest_dir]
set -euo pipefail
BK="${1:-$HOME/dev/tentacle-trader-backups}"
mkdir -p "$BK"
TS="$(date +%Y%m%d-%H%M%S)"
echo "Pulling /app/state from Railway (base64-safe stream so binaries survive)..."
railway ssh "tar czf - -C /app state | base64" > "$BK/state-$TS.tgz.b64"
base64 --decode "$BK/state-$TS.tgz.b64" > "$BK/state-$TS.tgz"
rm "$BK/state-$TS.tgz.b64"
echo "Saved: $BK/state-$TS.tgz ($(ls -lh "$BK/state-$TS.tgz" | awk '{print $5}'))"
tar tzf "$BK/state-$TS.tgz" | grep -E 'knowledge.json$|outcomes.jsonl$' >/dev/null && echo "verified: brain files present ✔"
