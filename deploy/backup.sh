#!/bin/sh
# Consistent copy of aidr.db into /data/backups inside the volume; keeps 14 days.
# Cron on the server: 15 3 * * * $HOME/aidr-stack/backup.sh
set -eu
cd "$(dirname "$0")"
docker compose exec -T aidr-obs python - "$(date +%Y%m%d-%H%M%S)" <<'EOF'
import os, pathlib, sqlite3, sys, time

backups = pathlib.Path("/data/backups")
backups.mkdir(mode=0o700, exist_ok=True)
dest = backups / f"aidr-{sys.argv[1]}.db"
src = sqlite3.connect("/data/aidr.db")
dst = sqlite3.connect(dest)
src.backup(dst)
dst.close()
src.close()
os.chmod(dest, 0o600)
cutoff = time.time() - 14 * 86400
for old in backups.glob("aidr-*.db"):
    if old.stat().st_mtime < cutoff:
        old.unlink()
print(dest)
EOF
