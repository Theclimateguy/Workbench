#!/bin/bash
cp /etc/resolv.conf /root/.resolv.conf.orig 2>/dev/null || true
printf 'nameserver 10.255.255.1\noptions timeout:1 attempts:1\n' > /etc/resolv.conf
