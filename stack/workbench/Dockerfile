# Workbench sandbox: where the agent runs its own scripts against field devices (PLAN.md §4 phase A).
# Built once with internet by `support-ctl workbench build` (bootstrap phase "workbench-image"); afterwards
# runs offline. No GPU, no host files; target_host.py passes through only the devices the operator approved.
FROM python:3.12-slim-bookworm
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      can-utils iproute2 iputils-ping curl socat usbutils picocom nmap netcat-openbsd \
      openssh-client jq i2c-tools libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*
# Protocol libraries the agent may import (versions pinned in versions.lock -> build args)
ARG PYSERIAL=3.5 PYTHON_CAN=4.6.1 PYMODBUS=3.16.1 SCAPY=2.8.0 ESPTOOL=5.5.0 PARAMIKO=5.0.0 REQUESTS=2.34.2 PYUSB=1.3.1 CANTOOLS=44.2.1 HTTPX=0.28.1
RUN pip install "pyserial==$PYSERIAL" "python-can==$PYTHON_CAN" "pymodbus==$PYMODBUS" "scapy==$SCAPY" \
      "esptool==$ESPTOOL" "paramiko==$PARAMIKO" "requests==$REQUESTS" "pyusb==$PYUSB" "cantools==$CANTOOLS" "httpx==$HTTPX"
WORKDIR /work
CMD ["python3", "/work/script.py"]
