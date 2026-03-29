# Error 101 - Network Timeout: Troubleshooting Guide

**Document ID:** TS-ERR-101
**Severity:** High
**Affected systems:** DeviceA-X200, DeviceB-G500, BMO Core Banking Gateway
**Last updated:** 2025-03-01
**Owner:** BMO Network Operations Centre (NOC)

---

## Overview

**Error 101 (Network Timeout)** is raised when a device fails to receive a
response from a remote host within the configured timeout window. It is one of
the most common errors encountered in BMO branch and data centre environments
and is typically caused by one of the following root causes:

1. Physical or link-layer connectivity loss
2. Routing or firewall policy misconfiguration
3. Remote service or host unavailability
4. Network congestion causing packet drops
5. DNS resolution failure

---

## Symptoms

- Application displays "Connection timed out" or "Error 101" in the UI.
- Ping to gateway or remote host fails or shows 100% packet loss.
- `traceroute` stops at a specific hop with no response (`* * *`).
- BMO monitoring alerts: `ALERT: host_unreachable` or `ALERT: latency_threshold_exceeded`.
- Device logs contain: `ERR [net] connect timeout after 30000ms - error_code=101`

---

## Diagnostic Steps

### Step 1 - Verify local connectivity

```bash
# Check interface is up and has the correct IP
ip addr show eth0

# Ping default gateway
ping -c 4 $(ip route show default | awk '{print $3}')

# Check for packet loss on the uplink
mtr --report --report-cycles 20 <gateway-ip>
```

**Expected:** Zero packet loss to the default gateway. If packet loss is seen
at hop 1, the issue is local (cable, switch port, NIC).

### Step 2 - DNS resolution

```bash
# Test DNS resolution
dig +short bmo.internal @10.0.0.53

# If no response, check DNS server reachability
nc -zv 10.0.0.53 53
```

**Expected:** DNS should resolve within 50ms. Timeouts here indicate the DNS
server (10.0.0.53) is unreachable - check the management VLAN route.

### Step 3 - Route verification

```bash
# Show routing table
ip route show table main

# Trace path to target host
traceroute -n <target-ip>
```

Look for:
- Missing default route (`0.0.0.0/0`) - gateway not configured or route withdrawn.
- Routing loop (same IP appearing multiple times in traceroute).
- Asymmetric path (packets going out but not returning).

### Step 4 - Firewall / ACL check

```bash
# On DeviceA-X200 / DeviceB-G500
show access-list summary
show policy-map stats

# Check for blocked sessions in real time
debug firewall session | grep <target-ip>
```

Common firewall causes:
- Port 443/8443 blocked outbound (required for BMO API gateway).
- ICMP blocked by ACL (prevents ping but not TCP).
- Rate-limiting rule triggered by a burst of connections.

### Step 5 - Remote host availability

```bash
# TCP connectivity test (e.g. to BMO Core Banking on port 3000)
nc -zv core-banking.bmo.internal 3000

# HTTPS health check
curl -v --max-time 10 https://api.bmo.internal/health
```

If the remote host is unreachable, escalate to the team responsible for that
service. Do **not** make firewall changes without Change Advisory Board (CAB)
approval.

---

## Resolution Procedures

### Resolution A - Physical/Link Layer

1. Inspect cable and SFP transceiver at both ends.
2. Check switch port status: `show interface GigabitEthernet0/1` on the upstream switch.
3. Replace cable or transceiver if errors > 0.
4. If the switch port shows `err-disabled`, run: `interface GigabitEthernet0/1` -> `shutdown` -> `no shutdown`.

### Resolution B - Routing Issue

1. Re-add missing default route:

   ```bash
   ip route add default via <gateway-ip> dev eth0
   ```

2. If route is being withdrawn by BGP/OSPF, check neighbour adjacency:

   ```bash
   show bgp summary
   show ospf neighbor
   ```

3. For persistent route loss, open a P2 ticket with **Network Engineering**.

### Resolution C - DNS Failure

1. Temporarily override DNS for testing:

   ```bash
   echo "nameserver 8.8.8.8" > /etc/resolv.conf.test
   dig +short bmo.internal @8.8.8.8
   ```

2. If external DNS works but internal does not, escalate to **DNS/IPAM team**.
3. Do **not** leave public DNS configured in production - restore internal DNS.

### Resolution D - Firewall Rule

1. Identify the blocking rule in the FMC policy hit counts.
2. Raise a Change Request in ServiceNow (category: Network / Firewall).
3. CAB approval required for any production firewall change.
4. Apply rule after approval and confirm connectivity within the change window.

---

## Escalation Matrix

| Severity | Condition | Escalate To | Target Response |
|---|---|---|---|
| P1 | Core banking unreachable | NOC -> Network Eng -> CIO | 15 min |
| P2 | Branch connectivity down | NOC -> Network Eng | 1 hour |
| P3 | Single service timeout | Service owner team | 4 hours |
| P4 | Non-critical timeout | Standard ticket queue | Next business day |

---

## Related Errors

- **Error 102 - DNS Resolution Failure**: See TS-ERR-102
- **Error 110 - TLS Handshake Timeout**: See TS-ERR-110
- **Error G500-TUN-001 - IPsec Tunnel Down**: See DeviceB-G500 Manual, Section 5.1

---

## Change Log

| Version | Date | Author | Notes |
|---|---|---|---|
| 1.0 | 2023-06-15 | J. Chen (NOC) | Initial release |
| 1.4 | 2024-01-10 | A. Patel (Net Eng) | Added SD-WAN path failover steps |
| 2.0 | 2025-03-01 | M. Torres (NOC) | Updated for DeviceB-G500 support |
