# evalbox — a disposable Ubuntu 24.04 "workstation" with systemd + sshd, used as the agent's target by
# tests/eval/stand.sh (PLAN.md §5). Faults are injected by cases/*/inject.sh, fixed by the agent, verified by check.sh.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive container=docker
RUN apt-get update && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus openssh-server sudo python3 python3-venv iproute2 iputils-ping curl ca-certificates \
      vim-tiny less jq logrotate cron rsyslog netplan.io net-tools lsof psmisc htop apt-utils \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update && apt-get download hello 2>/dev/null && mkdir -p /opt/eval && mv hello_*.deb /opt/eval/hello.deb || true
RUN systemctl enable ssh cron rsyslog \
    && mkdir -p /run/sshd \
    && rm -f /lib/systemd/system/multi-user.target.wants/getty* /lib/systemd/system/sysinit.target.wants/systemd-udev* \
    && printf 'PermitRootLogin no\n' > /etc/ssh/sshd_config.d/10-eval.conf
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
