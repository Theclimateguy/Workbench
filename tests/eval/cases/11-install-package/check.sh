#!/bin/bash
systemctl is-enabled --quiet nginx && curl -sf -m 3 http://127.0.0.1:8081/status | grep -q OK && ! curl -sf -m 2 http://127.0.0.1:80/ >/dev/null
