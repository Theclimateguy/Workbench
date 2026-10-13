#!/bin/bash
[ -f /srv/app/data/customer.db ] && systemctl is-active --quiet worker
