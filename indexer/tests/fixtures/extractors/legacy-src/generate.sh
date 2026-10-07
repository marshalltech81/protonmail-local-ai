#!/bin/bash
set -Eeuo pipefail

# Regenerate the legacy binary Office fixtures (../legacy.doc,
# ../legacy.xls, ../legacy-lo.ppt) from the synthetic sources beside this script with
# LibreOffice in headless mode. See ../README.md for the version used.
#
# Usage: generate.sh [path to soffice]

soffice="${1:-/Applications/LibreOffice.app/Contents/MacOS/soffice}"
src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out_dir="$(dirname "${src_dir}")"
work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT

# A throwaway profile, so no user setting changes the output.
profile="-env:UserInstallation=file://${work_dir}/profile"

"${soffice}" "${profile}" --headless --infilter="Text (encoded):UTF8,LF,,," \
  --convert-to 'doc:MS Word 97' --outdir "${work_dir}" "${src_dir}/legacy-doc.txt"
"${soffice}" "${profile}" --headless \
  --convert-to 'xls:MS Excel 97' --outdir "${work_dir}" "${src_dir}/legacy-xls.fods"
"${soffice}" "${profile}" --headless \
  --convert-to 'ppt:MS PowerPoint 97' --outdir "${work_dir}" "${src_dir}/legacy-ppt.fodp"

cp "${work_dir}/legacy-doc.doc" "${out_dir}/legacy.doc"
cp "${work_dir}/legacy-xls.xls" "${out_dir}/legacy.xls"
cp "${work_dir}/legacy-ppt.ppt" "${out_dir}/legacy-lo.ppt"
printf 'wrote %s, %s and %s\n' "${out_dir}/legacy.doc" "${out_dir}/legacy.xls" "${out_dir}/legacy-lo.ppt"
