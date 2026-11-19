#!/bin/sh
# Bind-mounted ./logs and ./staging are created by Docker as root.
# Fix ownership, then run the crawler as uid 1000.
set -eu

if [ "$(id -u)" = "0" ]; then
  mkdir -p /data/staging /data/logs
  chown crawler:crawler /data/staging /data/logs
  for f in /data/logs/crawler.log /data/logs/crawler.log.[0-9]*; do
    if [ -e "$f" ]; then
      chown crawler:crawler "$f"
    fi
  done
  exec python - "$@" <<'PY'
import os
import pwd
import sys

pw = pwd.getpwnam("crawler")
os.initgroups(pw.pw_name, pw.pw_gid)
os.setgid(pw.pw_gid)
os.setuid(pw.pw_uid)
os.environ["HOME"] = pw.pw_dir
os.execv(
    sys.executable,
    [sys.executable, "-m", "ddosecrets_crawler", *sys.argv[1:]],
)
PY
fi

exec python -m ddosecrets_crawler "$@"
