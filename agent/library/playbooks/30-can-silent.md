# Playbook: CAN bus is silent or nodes do not answer (шина CAN молчит, узлы не отвечают)

## Facts (READ)
- `scan` → CAN interface present (`can0`, USB-CAN adapter such as CANable / PCAN)?
- `observe dump` 10–20 s: frame count by id, link state (ERROR-ACTIVE / ERROR-PASSIVE / BUS-OFF), error counters.
- Expected bitrate from the device manual: `library_search "<device> CAN bitrate"`. Common: 125k, 250k, 500k, 1M.

## Decision tree
1. **No frames, state ERROR-ACTIVE, counters 0** → nobody transmits: node unpowered, wrong pins, or our adapter
   is the only node. Ask the operator: is the device powered, which connector/pins (CAN_H, CAN_L, GND)?
2. **Error counters climb / ERROR-PASSIVE / BUS-OFF** → physical layer or bitrate:
   - Bitrate mismatch is the most common cause. Re-init at the documented bitrate (`device_recover` restarts the
     interface; changing bitrate in the device file is CONFIG).
   - Termination: exactly two 120 Ω resistors at the ends; with power off, CAN_H–CAN_L should measure ~60 Ω.
   - CAN_H / CAN_L swapped, missing common ground.
3. **Frames visible but the node ignores requests** → protocol: wrong id/format (11- vs 29-bit), CANopen node
   not in OPERATIONAL (NMT), J1939 address claim, request needs a specific DLC. Find the DBC/EDS in the library.

## Writing to the bus
- Every transmit is ACTUATION. Before sending: id, data, expected effect, what if it is wrong (rollback frame or
  power-cycle). Prefer read-type requests (SDO upload, J1939 request PGN) before any command frame.
- With a DBC file: decode with cantools in the sandbox (`workbench_run`, network none, CAN passed through).
