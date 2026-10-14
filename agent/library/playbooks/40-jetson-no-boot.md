# Playbook: NVIDIA Jetson does not boot (Jetson не загружается)

Jetson Orin / Xavier / Nano modules on dev kits or carrier boards.

## Facts
- Serial debug console is the primary source: micro-USB / USB-C "debug" port or the UART header (115200 8N1).
  `scan` → `/dev/serial/by-id/*` → register as `serial` device → `observe` while the operator power-cycles.
- Power LED, fan, HDMI output — ask the operator to describe; photos help (Media tab).
- If it reaches Linux: `collect_facts`, `cat /etc/nv_tegra_release`, `journalctl -b -1 -p err` (previous boot).

## Where it stops (from the console)
| Last console lines | Meaning | Next step |
|---|---|---|
| Nothing at all | no power / wrong UART pins / wrong baud | check supply (voltage, current rating), TX/RX swap, try 115200 |
| UEFI / bootloader, then "boot device not found" | rootfs medium (eMMC, NVMe, SD) missing or corrupted | check the medium, boot order in UEFI menu (operator, keyboard on console) |
| Kernel panic "VFS: Unable to mount root fs" | wrong `root=` / corrupted rootfs / missing NVMe driver | boot from another medium; fsck from recovery |
| Boot loops after an apt upgrade | L4T/JetPack mismatch with bootloader | re-flash with SDK Manager (operator's PC) — ACTUATION, confirm image and backup |
| Stuck at `systemd` target, emergency mode | `fstab` or service failure | see playbook "Linux service"; fix fstab from emergency shell |

## Recovery mode / flashing (ACTUATION)
- Force recovery: hold REC (FC REC) while pressing power/reset; host sees `lsusb` id `0955:7xxx`.
- Flashing erases the device. Only with the operator's confirmed JetPack version and board model; save user data
  first if the medium is readable. Flashing is done from an x86 Ubuntu host with SDK Manager — not from Workbench.
