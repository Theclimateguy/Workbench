#!/bin/bash
set -e
mkdir -p /var/log/app
cat > /usr/local/bin/chatty <<'S'
#!/bin/bash
while true; do echo "$(date +%T) DEBUG frame=$RANDOM payload=$(head -c 200 /dev/zero | tr '\0' x)" >> /var/log/app/chatty-debug.log; sleep 0.2; done
S
chmod +x /usr/local/bin/chatty
cat > /etc/systemd/system/chatty.service <<'U'
[Unit]
Description=Chatty sensor bridge (debug logging left on)
[Service]
ExecStart=/usr/local/bin/chatty
Restart=always
[Install]
WantedBy=multi-user.target
U
systemctl daemon-reload; systemctl enable --now chatty >/dev/null 2>&1
# old rotated junk + one huge debug file: ~230 of 256 MB
dd if=/dev/zero of=/var/log/app/chatty-debug.log.1 bs=1M count=120 status=none
dd if=/dev/zero of=/var/log/app/chatty-debug.log bs=1M count=100 status=none
