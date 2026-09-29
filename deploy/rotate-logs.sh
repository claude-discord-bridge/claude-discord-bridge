#!/bin/bash
# Rotates claude-discord-bridge's launchd-redirected logs and restarts the
# service so it starts writing to fresh files.
#
# Why a restart instead of the usual newsyslog rename-and-signal dance:
# launchd opens bot.log/bot.err.log itself and hands the open file
# descriptors to the bridge process for the life of that process. Renaming
# the file out from under it (what newsyslog normally does) does not make
# the process start writing to a new file -- it keeps appending to the
# renamed (now-invisible) inode via the fd it already has, so nothing is
# ever reclaimed until the process reopens its logs. Reopening on a signal
# would require the bridge to handle SIGHUP itself, which it doesn't (and
# adding that is an application change, not a deploy concern). Restarting
# the job is the reliable way to get a process holding a fresh fd at the
# same path: launchd reopens StandardOutPath/StandardErrorPath on every
# process start. See README.md's "Log rotation" section.
#
# No compression here either, for the same reason: compressing a file the
# still-running process might still be appending to risks corrupting the
# archive. Plain rename, capped generation count, restart, done.
set -euo pipefail

DIR="$HOME/.claude-discord"
KEEP=5

# audit.log is included even though it is not a launchd-redirected log: it
# gets one JSON line per approval DECISION, auto-allowed Reads included, so
# on an ordinary working day it outgrows bot.log and nothing else ever
# reclaims it. It differs in one way that does not matter here -- the bridge
# opens and closes it per entry rather than holding an fd, so a plain rename
# would be enough for this file on its own, no restart required. It rides
# along with the same generation cap for simplicity.
for name in bot.log bot.err.log audit.log; do
  f="$DIR/$name"
  [ -f "$f" ] || continue

  i="$KEEP"
  while [ "$i" -gt 1 ]; do
    prev=$((i - 1))
    if [ -f "$f.$prev" ]; then
      mv -f "$f.$prev" "$f.$i"
    fi
    i="$prev"
  done
  mv -f "$f" "$f.1"
done

launchctl kickstart -k "gui/$(id -u)/com.claude-discord"
