#!/bin/bash
# Copy agent-stack's session transcript + audit log out of running Harbor task containers into the
# job folder (harbor tears the container down when a trial ends). Only containers named
# "<trial>__env-main-1" for a trial directory of THIS job are touched; nothing is modified.
#   bench/collect_transcripts.sh <job-dir>
job=$1
while pgrep -f "venv/bin/harbor" >/dev/null; do
  for dir in "$job"/*__*/; do
    trial=$(basename "$dir"); c="${trial}__env-main-1"
    docker ps --format '{{.Names}}' | grep -qx "$c" || continue
    state=$(docker exec "$c" sh -c 'for d in /app /workspace /root /home/* /; do [ -d "$d/.agent" ] && { echo "$d/.agent"; break; }; done' 2>/dev/null)
    [ -n "$state" ] && { mkdir -p "$dir/agent-state"; docker cp "$c:$state/." "$dir/agent-state/" >/dev/null 2>&1; }
  done
  sleep 20
done
