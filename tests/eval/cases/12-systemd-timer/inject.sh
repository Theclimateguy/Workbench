#!/bin/bash
printf '#!/bin/bash\ntar czf /var/backups/config-$(date +%%s).tgz /etc/app 2>/dev/null; ls -t /var/backups/config-*.tgz | tail -n +6 | xargs -r rm\n' > /usr/local/bin/backup-config.sh
chmod +x /usr/local/bin/backup-config.sh; mkdir -p /etc/app /var/backups; echo "key=1" > /etc/app/app.conf
