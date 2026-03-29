# Error 200 — Firmware Update Failure: Troubleshooting Guide

**Document ID:** TS-ERR-200
**Severity:** Medium
**Affected systems:** DeviceA-X200, DeviceB-G500, DeviceC-M100, DeviceD-SW48
**Last updated:** 2025-06-01
**Owner:** BMO Infrastructure Engineering

---

## Overview

Error 200 is raised when a scheduled or manual firmware upgrade fails to complete. The device may be left in an inconsistent state with a partial firmware image. This guide covers the most common failure scenarios and safe recovery procedures.

Firmware updates can fail due to:
- Insufficient storage space on the device flash partition
- Network interruption during image download from the firmware repository
- Image checksum mismatch (corrupted download)
- Incompatible firmware version for the hardware revision
- Active sessions preventing the device from entering maintenance mode

---

## Symptoms

- Console log: `ERR [firmware] update failed — error_code=200`
- Device health dashboard shows: **Firmware: Degraded**
- Running `show version` displays mismatched active/standby firmware banks
- Device reboot loop if partial image was written to the active bank

---

## Diagnostic Steps

### Step 1 — Check available storage

```bash
# Verify flash storage availability
df -h /flash

# Minimum required free space: 256 MB
# If insufficient, remove old firmware images
ls /flash/firmware/
rm /flash/firmware/<old-image>.bin
```

### Step 2 — Verify firmware image integrity

Every firmware image ships with a SHA-256 checksum file. Always verify before applying.

```bash
# Download image and checksum
wget https://firmware.bmo-internal.net/releases/deviceD-sw48-v4.2.1.bin
wget https://firmware.bmo-internal.net/releases/deviceD-sw48-v4.2.1.sha256

# Verify checksum
sha256sum -c deviceD-sw48-v4.2.1.sha256

# Expected output:
# deviceD-sw48-v4.2.1.bin: OK
```

If the checksum fails, the image is corrupted. Re-download and verify again before proceeding.

### Step 3 — Check hardware revision compatibility

```bash
# Display hardware revision
show hardware revision

# Cross-reference against the firmware release notes at:
# https://firmware.bmo-internal.net/releases/release-notes-v4.2.1.txt
#
# Hardware revision A1–A4: supported on v4.2.x
# Hardware revision B1+:   requires v4.3.x or later
```

Applying a firmware image to an incompatible hardware revision will cause Error 200 and may brick the device.

### Step 4 — Terminate active sessions

Firmware upgrades require the device to enter maintenance mode. Active management sessions block this transition.

```bash
# List active sessions
show users

# Terminate all active sessions except your own
clear line vty 0 4
```

### Step 5 — Retry upgrade in maintenance window

```bash
# Stage the firmware image to the standby bank
copy tftp://10.0.0.5/deviceD-sw48-v4.2.1.bin flash:standby/

# Verify the staged image
verify flash:standby/deviceD-sw48-v4.2.1.bin

# Schedule activation on next reboot
boot system flash:standby/deviceD-sw48-v4.2.1.bin

# Reboot to activate (schedule for maintenance window)
reload at 02:00 "Scheduled firmware upgrade to v4.2.1"
```

---

## Recovery from Partial Firmware Write

If the device is stuck in a reboot loop after a failed upgrade:

1. Connect via serial console (9600 baud, 8N1)
2. Interrupt boot sequence by pressing **Ctrl+Break** within 5 seconds of power-on
3. At the ROM monitor prompt:

```
rommon> boot flash:backup/factory-default.bin
```

4. Once booted to factory image, re-apply the correct firmware following the steps above.

---

## Resolution Summary

| Root Cause | Resolution |
|---|---|
| Insufficient storage | Remove old firmware images from `/flash/firmware/` |
| Corrupted image | Re-download; verify SHA-256 checksum before applying |
| Hardware incompatibility | Check hardware revision; download correct firmware version |
| Active sessions blocking | Run `clear line vty 0 4` before initiating upgrade |
| Reboot loop after partial write | Boot from factory image via serial console ROM monitor |

---

## Escalation

If recovery steps fail or the device is unresponsive after two reboot attempts, escalate immediately:

- **On-call Infrastructure:** infra-oncall@bmo.com (P1 bridge: 1-800-BMO-INFRA)
- **Vendor TAC:** Reference device serial number and firmware version

Do not attempt a third upgrade without vendor guidance — three failed upgrades trigger a permanent lockout on affected hardware revisions.

---

## Related Documents

- TS-ERR-101: Network Timeout Troubleshooting Guide
- TS-ERR-102: Authentication Failure Guide
- DeviceD-SW48 Hardware Installation Manual
