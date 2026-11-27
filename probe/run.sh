#!/bin/bash
# One-off check of official sites: what pages and files they offer (results go to the "probe" branch).
mkdir -p /tmp/probe/html /tmp/probe/files
UA="Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36"
while read -r name url; do
  code=$(curl -sL -m 40 -A "$UA" -o "/tmp/probe/html/$name.html" -w "%{http_code} %{size_download} %{url_effective}" "$url")
  echo "$name $code" >> /tmp/probe/summary.txt
  grep -oiE 'href="[^"]+"' "/tmp/probe/html/$name.html" | sed 's/href=//I;s/"//g' | sort -u > "/tmp/probe/html/$name.links"
done < probe/urls.txt
# download the trade statistics files linked from the customs page
for f in $(grep -iE '\.(xlsx?|pdf|zip)' /tmp/probe/html/customs_fts_page.links | head -20); do
  case "$f" in http*) u="$f";; *) u="https://www.customs.gov.np$f";; esac
  n=$(basename "${u%%\?*}"); curl -sL -m 120 -A "$UA" -o "/tmp/probe/files/$n" "$u"; echo "file $n $(stat -c %s /tmp/probe/files/$n)" >> /tmp/probe/summary.txt
done
cd /tmp/probe && git init -q -b probe && git add -A && git -c user.name=news-robot -c user.email=robot@local commit -qm probe
git push -qf "https://x-access-token:${GH_PUSH}@github.com/${REPO}" probe
