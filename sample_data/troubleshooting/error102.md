# Error 102 — Authentication Failure: Troubleshooting Guide

**Document ID:** TS-ERR-102
**Severity:** High
**Affected systems:** DeviceA-X200, DeviceB-G500, BMO Core Banking Gateway
**Last updated:** 2025-04-15
**Owner:** BMO Security Operations

---

## Overview

Error 102 indicates that a device or service failed to authenticate against the central Identity Provider (IdP). This error is raised when credentials are invalid, expired, or when the RADIUS/LDAP server is unreachable.

Common triggers:
- Expired service account password
- Clock skew between device and authentication server (NTP drift > 5 minutes)
- RADIUS shared secret mismatch
- LDAP bind account locked after failed attempts
- TLS certificate presented by the IdP has expired or is untrusted

---

## Symptoms

- Console log entry: `ERR [auth] authentication failed — error_code=102`
- Admin portal shows device status: **Unauthenticated**
- SSH and API access refused with `Permission denied (publickey,keyboard-interactive)`
- Syslog contains repeated `RADIUS Access-Reject` messages

---

## Diagnostic Steps

### Step 1 — Verify system clock synchronisation

Clock skew is the most common cause of Kerberos and token-based authentication failures.

```bash
# Check current time and NTP sync status
timedatectl status

# Confirm NTP peers are reachable
ntpq -p

# Force immediate sync if drift is detected
chronyc makestep
```

Expected: NTP offset < 500 ms. If offset exceeds 5 minutes, authentication tokens will be rejected.

### Step 2 — Test RADIUS connectivity

```bash
# Send a test authentication request to the RADIUS server
radtest <username> <password> <radius-server-ip> 1812 <shared-secret>

# Expected output on success:
# Received Access-Accept Id 1 from <radius-server-ip>:1812
```

If you receive `Access-Reject`, the shared secret is mismatched or the user account is locked.

### Step 3 — Check LDAP bind account status

```bash
# Attempt an LDAP bind using the service account
ldapsearch -x -H ldap://<ldap-server> -D "cn=svc-device,ou=service,dc=bmo,dc=com" -W -b "dc=bmo,dc=com" "(uid=testuser)"
```

A `49 Invalid credentials` result means the bind account password has expired or been reset.

### Step 4 — Inspect TLS certificate validity

```bash
# Check the certificate presented by the IdP
openssl s_client -connect <idp-hostname>:636 -showcerts </dev/null 2>/dev/null | openssl x509 -noout -dates
```

Ensure `notAfter` is in the future. If the certificate is expired, contact the PKI team to renew it.

### Step 5 — Review authentication logs

```bash
# On the device
tail -n 100 /var/log/auth.log | grep "error_code=102"

# On the RADIUS server
tail -n 100 /var/log/freeradius/radius.log | grep "Access-Reject"
```

---

## Resolution

| Root Cause | Resolution |
|---|---|
| NTP clock skew | Run `chronyc makestep` and verify NTP peers |
| Expired service account | Reset password in Active Directory; update device config |
| RADIUS shared secret mismatch | Re-enter shared secret on both device and RADIUS server |
| LDAP bind account locked | Unlock account in AD: `Unlock-ADAccount -Identity svc-device` |
| Expired IdP TLS certificate | Raise P1 ticket with BMO PKI team; renew certificate |

---

## Escalation

If the above steps do not resolve Error 102 within 30 minutes, escalate to:

- **Tier 2:** BMO Network Security team — ext. 4102
- **Tier 3:** Identity & Access Management (IAM) team — iam-support@bmo.com

Reference this document (TS-ERR-102) in all escalation tickets.

---

## Related Documents

- TS-ERR-101: Network Timeout Troubleshooting Guide
- TS-ERR-200: Firmware Update Failure Guide
- SEC-POL-001: BMO Security Policy — Credential Management
