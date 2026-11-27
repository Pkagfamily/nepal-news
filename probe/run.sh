#!/bin/bash
mkdir -p /tmp/probe/html /tmp/probe/files
UA="Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36"
while read -r name url; do
  code=$(curl -sL -m 40 -A "$UA" -o "/tmp/probe/html/$name.html" -w "%{http_code} %{size_download} %{url_effective}" "$url")
  echo "$name $code" >> /tmp/probe/summary.txt
done < probe/urls.txt
for f in $(grep -oiE 'href="[^"]+\.(xlsx?|pdf)"' /tmp/probe/html/customs_fts83_page.html | sed 's/href=//I;s/"//g' | head -14); do
  case "$f" in http*) u="$f";; *) u="https://www.customs.gov.np$f";; esac
  n=$(basename "${u%%\?*}"); curl -sL -m 120 -A "$UA" -o "/tmp/probe/files/$n" "$u"; echo "file $n $(stat -c %s /tmp/probe/files/$n)" >> /tmp/probe/summary.txt
done
for p in customs_satti ird_actual; do
  for f in $(grep -oiE 'href="[^"]+\.(pdf|jpe?g|png)"' /tmp/probe/html/$p.html | sed 's/href=//I;s/"//g' | head -2); do
    case "$f" in http*) u="$f";; *) u="https://$( [ $p = ird_actual ] && echo ird.gov.np || echo www.customs.gov.np)$f";; esac
    n=$(basename "${u%%\?*}"); curl -sL -m 60 -A "$UA" -o "/tmp/probe/files/$n" "$u"; echo "file $p $n $(stat -c %s /tmp/probe/files/$n)" >> /tmp/probe/summary.txt
  done
done
cd /tmp/probe && git init -q -b probe && git add -A && git -c user.name=news-robot -c user.email=robot@local commit -qm probe2
git push -qf "https://x-access-token:${GH_PUSH}@github.com/${REPO}" probe
