#!/bin/bash
mkdir -p /tmp/probe/html /tmp/probe/files
UA="Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36"
while read -r name url; do
  code=$(curl -sL -m 40 -A "$UA" -o "/tmp/probe/html/$name.html" -w "%{http_code} %{size_download} %{url_effective}" "$url")
  echo "$name $code" >> /tmp/probe/summary.txt
done < probe/urls.txt
i=0
for p in $(grep -oE '<link>[^<]+</link>' /tmp/probe/html/nrb_feed_en.html | sed 's/<[^>]*>//g' | grep '/red/' | head -2); do
  i=$((i+1)); curl -sL -m 40 -A "$UA" -o /tmp/probe/html/nrb_item$i.html "$p"; echo "item$i $p" >> /tmp/probe/summary.txt
  for f in $(grep -oiE 'href="[^"]+\.(xlsx?|pdf)"' /tmp/probe/html/nrb_item$i.html | sed 's/href=//I;s/"//g' | grep -i uploads | head -4); do
    n=$(basename "${f%%\?*}"); curl -sL -m 120 -A "$UA" -o "/tmp/probe/files/$n" "$f"; echo "file $n $(stat -c %s /tmp/probe/files/$n)" >> /tmp/probe/summary.txt
  done
done
cd /tmp/probe && git init -q -b probe && git add -A && git -c user.name=news-robot -c user.email=robot@local commit -qm probe3
git push -qf "https://x-access-token:${GH_PUSH}@github.com/${REPO}" probe
