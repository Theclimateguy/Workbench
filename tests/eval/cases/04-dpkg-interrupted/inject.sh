#!/bin/bash
set -e
[ -f /opt/eval/hello.deb ] || { echo "no /opt/eval/hello.deb in the image (built offline?)"; exit 1; }
dpkg --unpack /opt/eval/hello.deb >/dev/null 2>&1 || true
# simulate the interrupted configure step: dpkg thinks a configure is pending
printf '#!/bin/sh\nexit 1\n' > /var/lib/dpkg/info/hello.postinst; chmod +x /var/lib/dpkg/info/hello.postinst
dpkg --configure hello >/dev/null 2>&1 || true
# the "real" fix: the postinst is broken; a working one is what the package ships (no postinst at all)
