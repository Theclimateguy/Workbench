#!/bin/bash
dpkg --audit 2>/dev/null | grep -q . && { echo "dpkg --audit still reports packages"; exit 1; }
apt-get -s install hello >/dev/null 2>&1
