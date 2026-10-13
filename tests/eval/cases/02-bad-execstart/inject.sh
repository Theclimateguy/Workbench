#!/bin/bash
set -e
cat > /usr/local/bin/telemetrd <<'S'
#!/bin/bash
while true; do echo "telemetry tick $(date +%T)" >> /var/log/app/telemetry.log; sleep 5; done
S
chmod +x /usr/local/bin/telemetrd; mkdir -p /var/log/app
cat > /etc/systemd/system/telemetry.service <<'U'
[Unit]
Description=Field telemetry uploader
After=network.target
[Service]
ExecStart=/usr/local/bin/telemetryd
Restart=on-failure
RestartSec=2
[Install]
WantedBy=multi-user.target
U
systemctl daemon-reload; systemctl enable telemetry >/dev/null 2>&1; systemctl start telemetry || true
