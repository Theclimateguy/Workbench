#!/bin/bash
! grep -q dead-beef /etc/fstab || grep -E '^UUID=00000000-dead-beef.*(nofail|x-systemd.automount)' /etc/fstab >/dev/null
