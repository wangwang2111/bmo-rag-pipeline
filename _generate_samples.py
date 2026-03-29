"""
_generate_samples.py
Generates the ADLS sample files:
  manuals/deviceA.pdf
  manuals/deviceB.pdf
  troubleshooting/error101.md
  policies/security.txt
Run once, then upload the output/ folder to your ADLS container.
"""

import os
from fpdf import FPDF

OUT = os.path.join(os.path.dirname(__file__), "sample_data")

def mkdir(path):
    os.makedirs(path, exist_ok=True)

# ── helpers ────────────────────────────────────────────────────────────────────

class DocPDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(80, 80, 80)
        self.cell(0, 8, self._header_title, align="R")
        self.ln(4)
        self.set_draw_color(180, 180, 180)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")

    def set_header_title(self, title):
        self._header_title = title

    def chapter_title(self, text):
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(0, 51, 102)
        self.ln(4)
        self.cell(0, 8, text)
        self.ln(10)
        self.set_text_color(0, 0, 0)

    def section_title(self, text):
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(30, 30, 30)
        self.ln(2)
        self.cell(0, 7, text)
        self.ln(8)
        self.set_text_color(0, 0, 0)

    def body(self, text):
        self.set_font("Helvetica", size=10)
        self.multi_cell(0, 6, text)
        self.ln(3)

    def bullet(self, items):
        self.set_font("Helvetica", size=10)
        indent = 8
        usable = self.w - self.l_margin - self.r_margin - indent
        for item in items:
            self.set_x(self.l_margin + indent)
            self.multi_cell(usable, 6, f"-  {item}")
        self.ln(2)

    def kv_table(self, rows):
        self.set_font("Helvetica", "B", 9)
        col_w = 55
        for k, v in rows:
            self.set_fill_color(240, 240, 240)
            self.cell(col_w, 7, k, border=1, fill=True)
            self.set_font("Helvetica", size=9)
            self.cell(0, 7, v, border=1)
            self.ln()
            self.set_font("Helvetica", "B", 9)
        self.ln(4)


# ── deviceA.pdf ────────────────────────────────────────────────────────────────

def make_deviceA():
    pdf = DocPDF()
    pdf.set_header_title("BMO FinTech - DeviceA-X200 User Manual  |  Rev 3.1")
    pdf.set_auto_page_break(auto=True, margin=15)

    # Cover
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(0, 51, 102)
    pdf.ln(20)
    pdf.cell(0, 12, "DeviceA-X200", align="C"); pdf.ln(12)
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "Hardware & Configuration Manual", align="C"); pdf.ln(8)
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, "Revision 3.1  |  March 2025  |  BMO Technology Operations", align="C")
    pdf.ln(6)
    pdf.set_draw_color(0, 51, 102)
    pdf.line(30, pdf.get_y(), 180, pdf.get_y())
    pdf.ln(14)
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(0, 6,
        "This manual provides installation, configuration, and maintenance "
        "procedures for the DeviceA-X200 network appliance. It is intended "
        "for certified BMO infrastructure engineers. Retain this document "
        "for the service life of the equipment.")

    # Chapter 1
    pdf.add_page()
    pdf.chapter_title("1. Product Overview")
    pdf.body(
        "The DeviceA-X200 is a rack-mounted network appliance designed for "
        "secure, high-throughput data processing in financial institution "
        "environments. It supports 10GbE and 25GbE uplinks, hardware-based "
        "AES-256 encryption, and dual redundant power supplies."
    )
    pdf.section_title("1.1 Key Specifications")
    pdf.kv_table([
        ("Model",           "DeviceA-X200"),
        ("CPU",             "Intel Xeon Silver 4314 - 16 cores @ 2.4 GHz"),
        ("RAM",             "64 GB DDR4 ECC (max 256 GB)"),
        ("Storage",         "2 × 960 GB NVMe SSD (RAID-1)"),
        ("Network",         "4 × 10GbE RJ45 + 2 × 25GbE SFP28"),
        ("Power",           "Dual 550W 80+ Platinum PSU"),
        ("Form factor",     "1U rack-mount (19 inch)"),
        ("Operating temp",  "0°C to 45°C"),
        ("Humidity",        "5% to 95% non-condensing"),
        ("Compliance",      "PCI-DSS 4.0, FIPS 140-2 Level 2"),
    ])
    pdf.section_title("1.2 Front Panel Components")
    pdf.bullet([
        "Power button with LED status indicator (green = on, amber = standby)",
        "USB 3.0 Type-A port (admin access only - disabled by policy after provisioning)",
        "LCD status display: hostname, IP, CPU/RAM utilisation",
        "Drive activity LEDs (1 per NVMe bay)",
        "Bezel lock with physical key",
    ])

    # Chapter 2
    pdf.add_page()
    pdf.chapter_title("2. Initial Setup")
    pdf.section_title("2.1 Rack Installation")
    pdf.body(
        "Use the supplied rail kit (part# RK-X200-19) to mount the appliance "
        "in a standard 19-inch rack. Allow a minimum of 1U clearance above "
        "and below for airflow. Do not obstruct the rear exhaust fans."
    )
    pdf.bullet([
        "Torque rack screws to 0.5 N·m - do not over-tighten.",
        "Ground the chassis to the rack PDU ground bar before powering on.",
        "Connect both PSU cables to separate PDU circuits for redundancy.",
    ])
    pdf.section_title("2.2 Network Configuration")
    pdf.body(
        "The device ships with DHCP enabled on port eth0. Connect eth0 to your "
        "management VLAN and note the assigned IP from the LCD display or your "
        "DHCP server lease table. Then connect via SSH to complete initial setup."
    )
    pdf.body("Default management credentials (change immediately after first login):")
    pdf.kv_table([
        ("Username",  "admin"),
        ("Password",  "BMO@X200-CHANGEME"),
        ("SSH port",  "22 (disable after configuring cert-based auth)"),
        ("Web UI",    "https://<ip>:8443"),
    ])
    pdf.section_title("2.3 IP Configuration via CLI")
    pdf.set_font("Courier", size=9)
    pdf.set_fill_color(245, 245, 245)
    pdf.multi_cell(0, 5,
        "# Set static IP on eth0\n"
        "nmcli con mod eth0 ipv4.method manual \\\n"
        "  ipv4.addresses 10.10.1.50/24 \\\n"
        "  ipv4.gateway 10.10.1.1 \\\n"
        "  ipv4.dns '8.8.8.8 8.8.4.4'\n"
        "nmcli con up eth0\n\n"
        "# Verify\n"
        "ip addr show eth0",
        fill=True,
    )
    pdf.ln(4)

    # Chapter 3
    pdf.add_page()
    pdf.chapter_title("3. Security Hardening")
    pdf.body(
        "All DeviceA-X200 units deployed within BMO must comply with the "
        "BMO Device Security Standard DS-2024-09 before being placed in "
        "production. The following steps are mandatory."
    )
    pdf.bullet([
        "Change default admin password (minimum 16 chars, mixed case, digits, symbols).",
        "Disable USB boot in UEFI BIOS and set BIOS password.",
        "Enable Secure Boot; import BMO signing certificate.",
        "Configure NTP to point to internal NTP servers (ntp1.bmo.internal, ntp2.bmo.internal).",
        "Enable audit logging to central syslog (syslog.bmo.internal:514 over TLS).",
        "Apply firmware update to version 3.1.7 or later (see Section 5).",
        "Register device in the BMO CMDB within 24 hours of deployment.",
    ])
    pdf.section_title("3.1 TLS Certificate Configuration")
    pdf.body(
        "Replace the self-signed certificate on the web management interface "
        "with a certificate signed by the BMO Internal CA. Use the following "
        "procedure:"
    )
    pdf.set_font("Courier", size=9)
    pdf.set_fill_color(245, 245, 245)
    pdf.multi_cell(0, 5,
        "# Generate CSR\n"
        "openssl req -new -newkey rsa:4096 -nodes \\\n"
        "  -keyout /etc/ssl/private/deviceA.key \\\n"
        "  -out /etc/ssl/certs/deviceA.csr \\\n"
        "  -subj '/CN=deviceA.bmo.internal/O=BMO/C=CA'\n\n"
        "# Submit CSR to BMO CA (PKI portal: https://pki.bmo.internal)\n"
        "# Install returned cert\n"
        "cp deviceA.crt /etc/ssl/certs/\n"
        "systemctl restart nginx",
        fill=True,
    )
    pdf.ln(4)

    # Chapter 4
    pdf.add_page()
    pdf.chapter_title("4. Maintenance & Monitoring")
    pdf.section_title("4.1 Health Check Commands")
    pdf.set_font("Courier", size=9)
    pdf.set_fill_color(245, 245, 245)
    pdf.multi_cell(0, 5,
        "# CPU and memory\n"
        "top -bn1 | head -5\n\n"
        "# Disk health\n"
        "smartctl -a /dev/nvme0n1\n\n"
        "# Network interface statistics\n"
        "ethtool -S eth0\n\n"
        "# Service status\n"
        "systemctl status bmo-agent\n\n"
        "# Hardware sensors\n"
        "ipmitool sdr list full",
        fill=True,
    )
    pdf.ln(4)
    pdf.section_title("4.2 Log Locations")
    pdf.kv_table([
        ("System log",        "/var/log/syslog"),
        ("BMO agent log",     "/var/log/bmo/agent.log"),
        ("Audit log",         "/var/log/audit/audit.log"),
        ("Network log",       "/var/log/bmo/network.log"),
        ("Firmware update",   "/var/log/bmo/firmware.log"),
    ])
    pdf.body(
        "Logs are rotated daily and retained locally for 7 days. All logs "
        "are also forwarded to the central syslog server in real time."
    )

    # Chapter 5
    pdf.add_page()
    pdf.chapter_title("5. Firmware Updates")
    pdf.body(
        "Firmware updates are distributed via the BMO Patch Management Portal "
        "(https://patches.bmo.internal). Always verify the SHA-256 checksum "
        "before applying an update. Schedule updates during approved maintenance "
        "windows only."
    )
    pdf.bullet([
        "Download the firmware bundle (.bfu) and its checksum file (.sha256).",
        "Verify: sha256sum -c deviceA-x200-3.1.7.bfu.sha256",
        "Apply via web UI: Administration > Firmware > Upload.",
        "The device will reboot automatically (approximately 4 minutes downtime).",
        "Verify version after reboot: show version (CLI) or Administration > About (UI).",
    ])

    path = os.path.join(OUT, "manuals", "deviceA.pdf")
    mkdir(os.path.dirname(path))
    pdf.output(path)
    print(f"  Created: {path}")


# ── deviceB.pdf ────────────────────────────────────────────────────────────────

def make_deviceB():
    pdf = DocPDF()
    pdf.set_header_title("BMO FinTech - DeviceB-G500 Gateway Manual  |  Rev 2.0")
    pdf.set_auto_page_break(auto=True, margin=15)

    # Cover
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(0, 51, 102)
    pdf.ln(20)
    pdf.cell(0, 12, "DeviceB-G500", align="C"); pdf.ln(12)
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "WAN Gateway - Installation & Operations Manual", align="C"); pdf.ln(8)
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, "Revision 2.0  |  January 2025  |  BMO Network Engineering", align="C")
    pdf.ln(6)
    pdf.set_draw_color(0, 51, 102)
    pdf.line(30, pdf.get_y(), 180, pdf.get_y())
    pdf.ln(14)
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(0, 6,
        "The DeviceB-G500 is BMO's primary WAN gateway appliance, providing "
        "secure branch-to-core connectivity using IPsec IKEv2 tunnels, SD-WAN "
        "path selection, and integrated next-generation firewall capabilities.")

    # Chapter 1
    pdf.add_page()
    pdf.chapter_title("1. Product Overview")
    pdf.body(
        "The G500 terminates up to 500 concurrent IPsec tunnels and provides "
        "multi-WAN failover across primary MPLS, secondary internet, and "
        "LTE backup links. It is deployed at all BMO branch offices with "
        "more than 20 employees."
    )
    pdf.section_title("1.1 Specifications")
    pdf.kv_table([
        ("Model",             "DeviceB-G500"),
        ("Throughput",        "5 Gbps encrypted / 10 Gbps cleartext"),
        ("IPsec tunnels",     "500 concurrent (IKEv2)"),
        ("WAN ports",         "2 × 1GbE (SFP) + 1 × LTE modem slot"),
        ("LAN ports",         "8 × 1GbE RJ45 (configurable VLANs)"),
        ("Console",           "RS-232 RJ45 (9600 baud 8N1)"),
        ("RAM",               "16 GB DDR4"),
        ("Flash",             "64 GB eMMC"),
        ("Operating temp",    "0°C to 50°C"),
        ("Compliance",        "PCI-DSS 4.0, SOC 2 Type II"),
    ])

    # Chapter 2
    pdf.add_page()
    pdf.chapter_title("2. WAN Configuration")
    pdf.section_title("2.1 Primary MPLS Link")
    pdf.body(
        "Connect the MPLS circuit to WAN-1 (SFP port). VLAN tagging is "
        "required; use the provider-assigned VLAN ID. Configure the interface "
        "as follows:"
    )
    pdf.set_font("Courier", size=9)
    pdf.set_fill_color(245, 245, 245)
    pdf.multi_cell(0, 5,
        "interface WAN1\n"
        "  description MPLS-PRIMARY\n"
        "  ip address 192.0.2.2/30\n"
        "  ip route-policy MPLS-POLICY in\n"
        "  encapsulation dot1q <provider-vlan>\n"
        "  no shutdown\n\n"
        "ip route 0.0.0.0/0 192.0.2.1 metric 10   ! primary default route",
        fill=True,
    )
    pdf.ln(4)
    pdf.section_title("2.2 IPsec Tunnel to BMO Core")
    pdf.body(
        "All branch traffic to the BMO data centre traverses an IKEv2 IPsec "
        "tunnel. The tunnel is established automatically using pre-shared keys "
        "provisioned via the Zero-Touch Provisioning (ZTP) service."
    )
    pdf.kv_table([
        ("IKE version",      "IKEv2"),
        ("Encryption",       "AES-256-GCM"),
        ("Integrity",        "SHA-384"),
        ("DH group",         "Group 21 (ECP-521)"),
        ("Lifetime (IKE)",   "86400 s"),
        ("Lifetime (IPsec)", "3600 s"),
        ("DPD interval",     "30 s"),
        ("Hub endpoint",     "vpn-hub.bmo.internal"),
    ])

    # Chapter 3
    pdf.add_page()
    pdf.chapter_title("3. SD-WAN Path Selection")
    pdf.body(
        "The G500 monitors WAN link quality continuously (latency, jitter, "
        "packet loss) and steers traffic to the best available path using "
        "application-aware routing policies."
    )
    pdf.section_title("3.1 SLA Thresholds")
    pdf.kv_table([
        ("Latency threshold",      "< 50 ms (critical apps)"),
        ("Jitter threshold",       "< 10 ms"),
        ("Packet loss threshold",  "< 0.5%"),
        ("Probe interval",         "every 5 s"),
        ("Failover trigger",       "3 consecutive probe failures"),
        ("Failback delay",         "60 s stable before switching back"),
    ])
    pdf.section_title("3.2 Traffic Classification")
    pdf.bullet([
        "Class CRITICAL: SWIFT, core banking (UDP/TCP 3000-3100) -> always MPLS",
        "Class BUSINESS: Citrix, VDI, voice (DSCP EF/AF41) -> MPLS preferred, internet fallback",
        "Class BULK: backups, software updates -> internet preferred, MPLS fallback",
        "Class DEFAULT: all other traffic -> load-balanced across available paths",
    ])

    # Chapter 4
    pdf.add_page()
    pdf.chapter_title("4. Firewall Policy")
    pdf.body(
        "The integrated NGFW enforces BMO's branch firewall policy. Rules are "
        "managed centrally from the Firewall Management Console (FMC) and pushed "
        "to all G500s via the policy distribution service. Local rule overrides "
        "are not permitted."
    )
    pdf.bullet([
        "Inbound to LAN: allow only traffic from BMO corporate subnets (10.0.0.0/8).",
        "Outbound to internet: deny by default; allow only via proxy (proxy.bmo.internal:8080).",
        "DNS: force all DNS to 10.0.0.53 (internal DNS resolver); block port 53 to internet.",
        "Geo-blocking: deny all traffic sourced from OFAC-sanctioned countries.",
        "IDS/IPS: inline prevention mode; signature set updated every 4 hours.",
    ])

    # Chapter 5
    pdf.add_page()
    pdf.chapter_title("5. Troubleshooting Common Issues")
    pdf.section_title("5.1 Tunnel Down (Error G500-TUN-001)")
    pdf.bullet([
        "Check WAN interface status: show interface WAN1",
        "Verify IKE negotiation: debug crypto ikev2",
        "Confirm pre-shared key matches hub configuration.",
        "Check NTP sync - certificate validation will fail if time drift > 5 min.",
        "Escalate to NOC if tunnel does not recover within 5 minutes.",
    ])
    pdf.section_title("5.2 High Latency on MPLS (Error G500-WAN-010)")
    pdf.body(
        "If SD-WAN probes report MPLS latency > 100ms sustained for > 5 minutes:"
    )
    pdf.bullet([
        "Run: traceroute -s <WAN1-IP> 10.0.1.1 to identify hop introducing latency.",
        "Check for QoS mis-marking: show policy-map WAN1 statistics.",
        "Open a ticket with MPLS provider if no internal QoS issue found.",
        "Manually force traffic to internet path if SLA violation confirmed: policy failover force WAN2",
    ])

    path = os.path.join(OUT, "manuals", "deviceB.pdf")
    mkdir(os.path.dirname(path))
    pdf.output(path)
    print(f"  Created: {path}")


# ── error101.md ────────────────────────────────────────────────────────────────

def make_error101():
    content = """\
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
"""
    path = os.path.join(OUT, "troubleshooting", "error101.md")
    mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Created: {path}")


# ── security.txt ───────────────────────────────────────────────────────────────

def make_security():
    content = """\
================================================================================
BMO TECHNOLOGY OPERATIONS
INFORMATION SECURITY POLICY - NETWORK DEVICE MANAGEMENT
Policy ID  : IS-NET-004
Version    : 5.2
Effective  : 2025-01-15
Review date: 2026-01-15
Owner      : Chief Information Security Officer (CISO)
Approver   : BMO Technology Risk Committee
Classification: INTERNAL - NOT FOR EXTERNAL DISTRIBUTION
================================================================================


1. PURPOSE
----------
This policy establishes the minimum security requirements for all network devices
operated by BMO Financial Group, including but not limited to firewalls, routers,
switches, WAN gateways, and network appliances. It applies to all BMO employees,
contractors, and third parties who manage or have access to network infrastructure.


2. SCOPE
--------
This policy applies to:

  a) All network devices owned or leased by BMO Financial Group.
  b) All devices operated on behalf of BMO by managed service providers.
  c) All network devices at BMO branch offices, data centres, and cloud environments.
  d) Virtual network devices (software-defined networking, cloud-native firewalls).

Out of scope: End-user workstations and mobile devices (governed by IS-END-001).


3. REGULATORY REFERENCES
------------------------
This policy supports compliance with the following regulations and standards:

  - OSFI Guideline B-10 (Third-Party Risk Management)
  - PCI DSS v4.0, Requirements 1, 2, 6, 10
  - NIST Cybersecurity Framework (CSF) v1.1
  - ISO/IEC 27001:2022, Annex A Controls 8.8, 8.9, 8.20
  - BMO Enterprise Risk Framework, Operational Risk Policy


4. POLICY REQUIREMENTS
-----------------------

4.1 AUTHENTICATION AND ACCESS CONTROL

  4.1.1 All network devices must use certificate-based or multi-factor
        authentication for administrative access. Password-only authentication
        is prohibited on internet-facing devices.

  4.1.2 Default vendor credentials (usernames and passwords) must be changed
        before a device is placed in production. No device may enter production
        with default credentials active.

  4.1.3 Administrative access must be restricted to designated management VLANs
        (10.255.0.0/16). Direct internet access to management interfaces is
        prohibited.

  4.1.4 Role-based access control (RBAC) must be configured with the principle
        of least privilege. Operators may only access devices relevant to their
        assigned network segments.

  4.1.5 Privileged accounts must be managed via the BMO Privileged Access
        Management (PAM) system (CyberArk). Shared accounts are prohibited.

  4.1.6 All administrative sessions must use encrypted protocols only:
          - SSH version 2 (TLS 1.3 for web management)
          - Telnet, HTTP, and SSHv1 are prohibited
          - SNMPv1 and SNMPv2c are prohibited; use SNMPv3 with AES-128 minimum

  4.1.7 Idle session timeout: 10 minutes for CLI sessions, 5 minutes for web UI.


4.2 CONFIGURATION MANAGEMENT

  4.2.1 All device configurations must be stored in the BMO Configuration
        Management Database (CMDB) and version-controlled via the Network
        Configuration Management (NCM) system (SolarWinds NCM).

  4.2.2 Configuration changes must follow the BMO Change Management Process
        (IS-CHG-001). Emergency changes require post-facto CAB review within
        24 hours.

  4.2.3 Running configurations must match approved baseline configurations.
        Automated drift detection runs every 6 hours; deviations generate a P3
        ticket automatically.

  4.2.4 Unused services and ports must be explicitly disabled. The permitted
        service list per device class is documented in the BMO Hardening Guide
        (IS-HRD-002).

  4.2.5 SNMP community strings must be non-guessable (minimum 20 characters,
        randomly generated) and rotated quarterly.


4.3 PATCH AND VULNERABILITY MANAGEMENT

  4.3.1 Critical and high-severity vulnerabilities (CVSS >= 7.0) must be
        remediated within:
          - Critical (CVSS >= 9.0): 30 days
          - High (CVSS 7.0-8.9): 90 days
          - Medium (CVSS 4.0-6.9): 180 days

  4.3.2 Vendor security advisories must be reviewed within 5 business days of
        publication and assessed for applicability.

  4.3.3 Firmware and software updates must be tested in the BMO Network Lab
        environment before production deployment.

  4.3.4 All patch activities must be performed during approved maintenance
        windows defined in the BMO Change Calendar.


4.4 LOGGING AND MONITORING

  4.4.1 All network devices must send logs to the BMO Security Information and
        Event Management (SIEM) system (Splunk) in real time via syslog over
        TLS to syslog.bmo.internal:6514.

  4.4.2 The following events must be logged at minimum:
          - All administrative logins (successful and failed)
          - Configuration changes
          - Interface state changes (up/down)
          - BGP/OSPF neighbour state changes
          - ACL/firewall deny events
          - High CPU/memory alerts (threshold: > 90% for > 5 minutes)

  4.4.3 Log retention: 90 days on-device (where storage permits), 13 months
        in the SIEM.

  4.4.4 NTP must be synchronised to internal NTP servers (ntp1.bmo.internal,
        ntp2.bmo.internal). Time drift > 5 seconds triggers an alert.

  4.4.5 Network traffic must be sampled and sent to the BMO NetFlow collector
        (netflow.bmo.internal:2055) for traffic analysis and anomaly detection.


4.5 PHYSICAL SECURITY

  4.5.1 Network equipment must be installed in locked, access-controlled
        telecommunications rooms or data centre cages.

  4.5.2 Physical access must be logged. Access logs are reviewed monthly by
        the Site Security team.

  4.5.3 USB and console ports must be physically disabled or blocked when
        not in active use. USB port lockdown must be enforced at the OS level
        in addition to physical controls.

  4.5.4 Decommissioned devices must have their configuration wiped and storage
        sanitised according to NIST SP 800-88 Rev. 1 before disposal.


4.6 ENCRYPTION

  4.6.1 All data in transit on WAN links must be encrypted using IPsec IKEv2
        with the following minimum parameters:
          - Encryption: AES-256-GCM
          - Integrity:  SHA-384
          - Key exchange: ECDH Group 21 (P-521) or Group 20 (P-384)

  4.6.2 TLS 1.0 and TLS 1.1 are prohibited on all management interfaces.
        TLS 1.2 with strong cipher suites is the minimum; TLS 1.3 is preferred.

  4.6.3 Certificates must be issued by the BMO Internal CA. Public certificates
        from approved CAs may be used for internet-facing services only with
        CISO approval.

  4.6.4 Private keys must be stored in hardware security modules (HSM) or
        encrypted key stores. Keys must not be stored in plaintext on disk.


5. EXCEPTIONS
-------------
Exceptions to this policy require written approval from the CISO and must be
documented in the Exception Register (Risk Register ID: IS-EXC). Exceptions
are time-limited (maximum 90 days, renewable once with CISO approval).


6. COMPLIANCE AND ENFORCEMENT
------------------------------
Compliance with this policy is monitored through:

  - Quarterly automated configuration compliance scans (IS-AUD-003)
  - Annual penetration testing of network infrastructure
  - Continuous SIEM alerting for policy violations

Non-compliance may result in:

  - Immediate device isolation from the network
  - Disciplinary action up to and including termination
  - Regulatory reporting obligations under OSFI and PCI DSS


7. RELATED DOCUMENTS
---------------------
  IS-CHG-001  Change Management Policy
  IS-END-001  Endpoint Security Policy
  IS-HRD-002  Device Hardening Standards
  IS-AUD-003  IT Audit and Compliance Procedure
  DeviceA-X200 Manual (Rev 3.1)
  DeviceB-G500 Manual (Rev 2.0)


8. CONTACT
----------
For questions or to report a security concern:
  Email : network-security@bmo.com
  SIEM  : security-alerts@bmo.com
  NOC   : +1-800-555-0199 (24/7)

================================================================================
END OF DOCUMENT  |  IS-NET-004 v5.2  |  INTERNAL USE ONLY
================================================================================
"""
    path = os.path.join(OUT, "policies", "security.txt")
    mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Created: {path}")


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Generating sample ADLS documents in: {OUT}/")
    make_deviceA()
    make_deviceB()
    make_error101()
    make_security()
    print("\nDone. Upload the contents of sample_data/ to your ADLS container.")
    print("  manuals/deviceA.pdf")
    print("  manuals/deviceB.pdf")
    print("  troubleshooting/error101.md")
    print("  policies/security.txt")
