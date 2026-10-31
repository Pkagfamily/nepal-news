#!/bin/bash
# Runs the engine again and again for up to ~50 minutes, publishing after every round.
# GitHub starts scheduled jobs late, so one long job (and the next one queued behind it)
# keeps the news fresh: a new round starts about every 3 minutes.
set -u
END=$(( $(date +%s) + ${LOOP_MINUTES:-50} * 60 ))
ROUND=0
while :; do
  START=$(date +%s)
  ROUND=$((ROUND + 1))
  echo "===== round $ROUND $(date -u +%H:%M:%S)" | tee -a /tmp/logs/engine.txt
  python3 scripts/engine.py > /tmp/logs/round.txt 2>&1
  CODE=$?
  cat /tmp/logs/round.txt >> /tmp/logs/engine.txt
  tail -c 200000 /tmp/logs/engine.txt > /tmp/logs/e.tmp && mv /tmp/logs/e.tmp /tmp/logs/engine.txt
  cp /tmp/logs/round.txt /tmp/logs/last_round.txt
  if [ $CODE -eq 0 ] && [ -f out/feed.bin ]; then
    T=$(mktemp -d)
    cp out/feed.bin out/latest.bin out/pulse.bin out/state.bin out/status.json "$T"/
    (cd "$T" && git init -q -b feed && git add -A &&
      git -c user.name=news-robot -c user.email=robot@local commit -qm "news $(date -u +%H:%M)" &&
      git push -qf "https://x-access-token:${GH_PUSH}@github.com/${REPO}" feed) || echo "publish failed"
    rm -rf "$T"
  fi
  cp out/status.json /tmp/logs/ 2>/dev/null || true
  (cd /tmp/logs && rm -rf .git && git init -q -b logs && git add -A &&
    git -c user.name=news-robot -c user.email=robot@local commit -qm "round $ROUND" &&
    git push -qf "https://x-access-token:${GH_PUSH}@github.com/${REPO}" logs) >/dev/null 2>&1 || true
  NOW=$(date +%s)
  [ $(( NOW + 180 )) -gt $END ] && break
  WAIT=$(( 180 - (NOW - START) ))
  [ $WAIT -gt 0 ] && sleep $WAIT
done
