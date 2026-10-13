#!/bin/bash
netplan generate >/dev/null 2>&1 && ip -4 -o addr show scope global | grep -q inet
