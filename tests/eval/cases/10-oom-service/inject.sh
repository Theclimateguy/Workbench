#!/bin/bash
cat > /usr/local/bin/cache-warm <<'S'
#!/usr/bin/python3
import time
buf = bytearray(180 * 1024 * 1024)   # the cache really needs ~180 MB
print("cache warm, serving", flush=True)
while True: time.sleep(5)
S
chmod +x /usr/local/bin/cache-warm
cat > /etc/systemd/system/cache-warm.service <<'U'
[Unit]
Description=Cache warmer
[Service]
ExecStart=/usr/local/bin/cache-warm
MemoryMax=64M
MemorySwapMax=0
Restart=always
RestartSec=1
[Install]
WantedBy=multi-user.target
U
systemctl daemon-reload; systemctl enable --now cache-warm >/dev/null 2>&1 || true
