#!/bin/bash
[ "$(date +%Y)" -ge 2026 ] && curl -sf -m 5 https://archive.ubuntu.com >/dev/null
