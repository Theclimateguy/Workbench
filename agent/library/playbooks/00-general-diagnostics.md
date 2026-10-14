# Playbook: general diagnostics of an unknown device (общая диагностика неизвестного устройства)

Use when the operator plugs in something and says "it does not work" with little context.

## 1. Inventory before hypotheses
- `scan`: what is physically connected — USB ids, `/dev/serial/by-id`, CAN interfaces, hosts on each link, mDNS names.
- `device_list`: what is already registered. Unknown USB-serial adapter → ask the operator what is on the other end.
- Identify the device class: Linux computer (SSH), microcontroller / PLC console (serial), bus node (CAN, Modbus),
  appliance with a web UI (HTTP). Pick the matching playbook.

## 2. Establish "alive" with the least invasive check
| Class | Alive check (READ) |
|---|---|
| Linux over SSH | `collect_facts` (uptime, failed units, disk, dmesg errors) |
| Serial console | `observe` 10–30 s; nothing → check baud (115200, 9600, 57600, 38400) and that TX/RX are not swapped |
| CAN | `observe dump` 10 s; no frames → bitrate, termination, bus-off state |
| Modbus | `observe id` or read one known holding register |
| HTTP | `observe GET /` |

## 3. Narrow down
- Write down the symptom precisely: what was expected, what happens, since when, what changed (update, wiring, power cut).
- Look for the manual: `library_search "<model> troubleshooting"`, `library_search "<error code>"`.
- One variable at a time. Keep a log of every check and result in the answer.

## 4. Before any change
- State the hypothesis and the evidence. Prefer the smallest reversible change.
- CONFIG: keep a backup (`cp file file.bak.<date>`) or note the previous value.
- ACTUATION (writes to serial/CAN/Modbus, power, firmware): say what physically happens, give the rollback plan.

## 5. Stop rules
- The same check fails twice → stop, report findings, ask the operator.
- No manual for a register map / CAN id → say you are probing blind and ask before any write.
