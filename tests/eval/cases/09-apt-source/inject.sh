#!/bin/bash
echo "deb [signed-by=/usr/share/keyrings/vendor.gpg] http://repo.vendor.invalid/ubuntu noble main" > /etc/apt/sources.list.d/vendor-tools.list
