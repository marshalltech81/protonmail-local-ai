#!/bin/bash
set -Eeuo pipefail

# Regenerate the PowerPoint-made legacy .ppt fixture (../legacy.ppt) with
# Microsoft PowerPoint for Mac, driven by legacy-ppt.applescript, then
# replace the "Last Saved By" name PowerPoint writes from the signed-in
# account (scrub-ppt-author.py). See ../README.md for the version used.
# macOS asks once to let the terminal control PowerPoint.
#
# Usage: generate-ppt.sh

src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out_dir="$(dirname "${src_dir}")"
work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT

osascript "${src_dir}/legacy-ppt.applescript" "${work_dir}/legacy.ppt"
uvx --with olefile==0.47 python -I "${src_dir}/scrub-ppt-author.py" "${work_dir}/legacy.ppt"

cp "${work_dir}/legacy.ppt" "${out_dir}/legacy.ppt"
printf 'wrote %s\n' "${out_dir}/legacy.ppt"
