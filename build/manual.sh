#!/usr/bin/env bash
# Builds docs/manual/GX10-operator-manual.pdf from operator-manual.md (pandoc + XeLaTeX, DejaVu fonts).
set -euo pipefail
cd "$(dirname "$0")/../docs/manual"
pandoc operator-manual.md -o GX10-operator-manual.pdf \
  --pdf-engine=xelatex --toc --toc-depth=2 -N \
  -V mainfont="DejaVu Sans" -V monofont="DejaVu Sans Mono" -V fontsize=10pt \
  -V geometry:margin=2cm -V papersize=a4 -V colorlinks=true -V linkcolor=blue \
  -V toc-title="Содержание" \
  -H <(printf '%s\n' '\usepackage{fvextra}' '\DefineVerbatimEnvironment{Highlighting}{Verbatim}{breaklines,commandchars=\\\{\}}' '\fvset{breaklines=true}' '\usepackage{etoolbox}' '\AtBeginEnvironment{longtable}{\small}' '\setlength{\emergencystretch}{3em}')
echo "built $(pwd)/GX10-operator-manual.pdf ($(du -h GX10-operator-manual.pdf | cut -f1))"
