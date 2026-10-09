#!/bin/sh
# Mirror public archive.org disk-image collections into a local, untracked corpus.
# usage: tools/fetch_corpus.sh [dest] [item ...]
set -eu
dest=${1:-artifacts/corpus}
[ $# -gt 0 ] && shift
items=${*:-C64_Preservation_Project_10th_Anniversary_Collection Commodore64DiskNIBG64 Draven1541Commodore64NIBG64DiskImages}
for item in $items; do
  mkdir -p "$dest/$item"
  curl -fsS "https://archive.org/metadata/$item" | python3 -c '
import json, sys, urllib.parse
item = sys.argv[1]
for f in json.load(sys.stdin)["files"]:
    if f.get("source") == "original" and not f["name"].endswith((".xml", ".sqlite", ".torrent", ".jpg")):
        print(f"url = \"https://archive.org/download/{item}/{urllib.parse.quote(f['"'"'name'"'"'])}\"")
        print(f"output = \"{f['"'"'name'"'"']}\"")
' "$item" > "$dest/$item/.curlrc"
  (cd "$dest/$item" && curl -fL -C - --create-dirs --progress-bar -K .curlrc)
done
