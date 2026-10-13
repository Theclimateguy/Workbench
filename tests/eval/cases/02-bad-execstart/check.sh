#!/bin/bash
systemctl is-active --quiet telemetry && systemctl is-enabled --quiet telemetry
