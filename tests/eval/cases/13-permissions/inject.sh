#!/bin/bash
id app >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin app
mkdir -p /srv/app/data; chown root:root /srv/app/data; chmod 700 /srv/app/data
cat > /usr/local/bin/appwriter <<'S'
#!/bin/bash
while true; do date +%s > /srv/app/data/heartbeat || exit 1; sleep 2; done
S
chmod +x /usr/local/bin/appwriter
cat > /etc/systemd/system/appwriter.service <<'U'
[Unit]
Description=App writer
[Service]
User=app
ExecStart=/usr/local/bin/appwriter
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
U
systemctl daemon-reload; systemctl enable --now appwriter >/dev/null 2>&1 || true
