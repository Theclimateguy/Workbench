# Playbook: Linux service does not start or keeps failing (сервис systemd не запускается, падает)

## Facts (READ)
- `collect_facts` → failed units, disk, memory, recent errors.
- `run_readonly`: `systemctl status UNIT --no-pager -l`, `journalctl -u UNIT -n 200 --no-pager`,
  `systemctl cat UNIT`, `systemctl show UNIT -p ExecStart,User,WorkingDirectory,Restart,Result,ExecMainStatus`.
- Since when: `journalctl -u UNIT --since "-2h"`; boot-related: `journalctl -b -p err`.

## Typical causes → evidence → fix (CONFIG unless noted)
| Cause | Evidence | Fix |
|---|---|---|
| Wrong ExecStart path / typo | `status=203/EXEC`, "No such file" | correct path in a drop-in (`systemctl edit UNIT`) or the unit, `daemon-reload` |
| Missing permissions / user | `status=217/USER`, "Permission denied" | create the user / fix ownership (`chown`, `chmod`) of exactly the paths in the log |
| Config syntax error | app log line with file:line | fix that line; validate with the app's own check (`nginx -t`, `sshd -t`, `named-checkconf`) |
| Port in use | "Address already in use" | `ss -ltnp` → stop the other process or change the port |
| Disk full | "No space left on device", `df -h` 100 % | see playbook "disk full": vacuum journal, rotate logs, remove caches — never user data |
| OOM | `dmesg` / journal "Out of memory: Killed process" | find the consumer; set `MemoryMax=` or reduce workers; check for leaks |
| Dependency down | "Dependency failed" | fix the dependency first (`systemctl list-dependencies UNIT`) |
| Restart loop limit | "Start request repeated too quickly" | fix root cause, then `systemctl reset-failed UNIT` |

## Change discipline
- Prefer drop-ins (`/etc/systemd/system/UNIT.d/override.conf`) over editing vendor units.
- Back up any file before `write_file`. After the change: `daemon-reload`, `restart`, then verify with
  `status` + the last 20 log lines + a functional check (port answers, health URL).
- If the fix needs a reboot or touches sshd/network, say so before doing it (it can cut your access).
