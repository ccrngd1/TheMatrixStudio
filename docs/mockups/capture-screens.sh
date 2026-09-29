#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Regenerates docs/screenshots/mobile-ui/*.png from the prototype.
#
# Needs a headless Chromium (Playwright's chrome-headless-shell works) and the mockups folder served
# over HTTP, because the harness drives the prototype through a same-origin iframe:
#
#   python3 -m http.server 8765 --directory docs/mockups &
#   docs/mockups/capture-screens.sh
#
# _shot.html turns animations off before capturing. Headless virtual time does not advance CSS
# animations, so without that every sheet is caught on the first frame of its slide-in, translucent.
set -euo pipefail

CHROME=${CHROME:-$(ls -d ~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell 2>/dev/null | tail -1)}
BASE=${BASE:-http://127.0.0.1:8765}
OUT=${OUT:-$(cd "$(dirname "$0")/.." && pwd)/screenshots/mobile-ui}
mkdir -p "$OUT"

enc() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

# cap NAME BUDGET_MS SRC STEPS [WIDTH HEIGHT SCALE]
# STEPS is "delay_ms@@js||delay_ms@@js", evaluated inside the prototype in order.
cap() {
  local w=${5:-390} h=${6:-844} d=${7:-2}
  timeout 90 "$CHROME" --no-sandbox --hide-scrollbars --force-device-scale-factor="$d" --window-size="$w,$h" \
    --virtual-time-budget="$2" --screenshot="$OUT/$1.png" \
    "$BASE/_shot.html?w=$w&h=$h&src=$(enc "$3")&do=$(enc "$4")" 2>/dev/null
  echo "$1.png"
}

cap 01-runs          3000  "prototype.html?noboot#/runs" "1200@@render()"
cap 02-conversation  9500  "prototype.html?noboot#/run/quiet-harbor" "8000@@render()"
cap 03-room-map      3000  "prototype.html?noboot#/run/trusted-robot/cast" "1200@@render()"
cap 04-analysis      3000  "prototype.html?noboot#/run/trusted-robot/analysis" "1200@@render()"
cap 05-dossier       3000  "prototype.html?noboot#/run/trusted-robot" "800@@A.dossier('trusted-robot','LP')"
cap 06-scrubber      3000  "prototype.html?noboot#/run/trusted-robot/scrub" "800@@S.scrub['trusted-robot']=8;render()"
cap 07-fork          3000  "prototype.html?noboot#/run/trusted-robot/scrub" \
  "800@@S.scrub['trusted-robot']=8;A.fork('trusted-robot')||200@@document.getElementById('forkText').value='The CFO joins and says the budget is frozen until Q3.'"
cap 08-asides        5000  "prototype.html?noboot#/run/trusted-robot" \
  "800@@A.asides('trusted-robot')||300@@A.askQuick('Why did Lena never move?')||1800@@render()"
cap 09-ensemble      15000 "prototype.html?noboot#/ensemble/pricing-tiers" "13000@@render()"
cap 10-wizard-cast   6000  "prototype.html?noboot#/runs" "600@@S.draft=blankDraft();A.importSample();go('new/2')||300@@A.openP('0')||2800@@render()"
cap 11-wizard-launch 6000  "prototype.html?noboot#/runs" "600@@S.draft=blankDraft();A.importSample();S.draft.kbs=['support'];go('new/5')||3000@@render()"
cap 12-knowledge     1500  "prototype.html?noboot#/knowledge/merchants" "150@@render()"
cap 13-wide          3000  "prototype.html?noboot&wide#/run/trusted-robot" "1200@@render()" 1500 900 1

timeout 90 "$CHROME" --no-sandbox --hide-scrollbars --force-device-scale-factor=1 --window-size=1266,792 \
  --virtual-time-budget=3000 --screenshot="$OUT/14-themes.png" "$BASE/_themes.html" 2>/dev/null
echo "14-themes.png"
