# Playbook: Modbus device does not answer (Modbus-устройство не отвечает)

## Facts (READ)
- TCP: device `HOST`, `PORT` (502), `UNIT` (slave id, often 1; gateways route by unit id).
- RTU: serial port, baud (9600/19200/38400), parity (often 8E1 or 8N1), unit id, RS-485 A/B wiring.
- Manual first: `library_search "<device> modbus register map"`, note whether addresses are 0- or 1-based
  (40001 = holding register 0 in most libraries).

## Decision tree
1. **TCP: connection refused / timeout** → reachability: is the host on the link (`scan`), right IP/port,
   firewall. Ping-like check: `observe id` or `observe holding 0 1`.
2. **TCP connects, every request times out** → wrong unit id (gateway drops it). Try the documented id, then 1, 0, 255.
3. **RTU: no reply** → wiring and framing: A/B swapped (try swapping — harmless), baud/parity mismatch, missing
   120 Ω termination on long lines, adapter needs a ground reference, unit id.
4. **Exception code 02 (illegal data address)** → register offset off by one or wrong table
   (holding vs input). Exception 01 → function not supported (use input/coil reads instead).
5. **Values look wrong** → scaling and word order: 32-bit values span two registers (big/little word order),
   floats IEEE-754, scale factors from the manual.

## Writes
- Every write (`write_register`, `write_coil`) is ACTUATION: the tool reads back the value. Before writing, state
  the register, old value (read it first), new value, physical effect, and the rollback (write the old value back).
