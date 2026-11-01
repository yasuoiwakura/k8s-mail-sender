# DNS Checklist

Date: 2026-10-31  
This file is a template. The DKIM public keys are only created during deployment and are **not** stored in Git.

## 1. Shared Records

| Purpose | Name | Type | Value | Action |
|---|---|---|---|---|
| Mail server host | `mx.example.com` | A | `203.0.113.10` | exists; update on IP change |
| Reverse DNS | `203.0.113.10` | PTR | `mx.example.com` | set; update at the hoster on IP change |
| TLS | `mx.example.com` | CAA | optionally `letsencrypt.org` | only check/add if CAA exists |
| IPv6 | `mx.example.com` | AAAA | - | do not create in the MVP; if present, clarify PTR/SPF separately |

`mx.example.com` is the only hostname that mail clients and remote MTAs see. A self-MX (`mx.example.com -> mx.example.com`) already exists, but in the MVP there is **no** SMTP service on TCP 25. The MX stays reserved for later inbound expansion and currently has no function.

### Why PTR (Reverse DNS)?

- PTR is the reverse lookup `IP -> hostname`. It lives in the reverse zone (`in-addr.arpa`) of the IP and is therefore set at the **VPS/server hoster**, not in the domain zone at your domain DNS provider. In the hoster panel it is called "Reverse DNS", "PTR" or "rDNS" depending on the provider; a support ticket is often needed. Domain DNS (A, MX, SPF, DKIM, DMARC) stays with the domain DNS provider.
- The PTR belongs to the **IP of the sending mail server**, not to the sender domains. So there is one PTR for `203.0.113.10 -> mx.example.com`, not four PTRs for the four sender domains.
- Receiving mail servers often check Forward-Confirmed Reverse DNS: `PTR -> hostname -> A -> same IP`.
- A missing, generic or non-matching PTR is not a hard SMTP error, but a strong reputation signal. Many providers classify such mail as spam or reject it.
- The PTR must match the **SMTP hostname** (`mx.example.com`), not every sender domain. All four domains use the same IP and the same HELO name.
- PTR is pure DNS information and requires **no open port**. Port 25 only needs to be open if the server is to accept inbound SMTP connections. For outbound-only, port 25 stays closed.
- For direct sending to recipient MXs, a correct PTR is practically required.
- When sending through a smarthost/relay, the PTR of the relay is relevant, not that of this server.

Currently the PTR points to `mx.example.com`, and the HELO name is also `mx.example.com`. Forward-Confirmed Reverse DNS is therefore consistent.

## 2. Records per Sender Domain

| Domain | SPF | DKIM | DMARC | MX |
|---|---|---|---|---|
| `example.org` | currently sufficient (VPS via `spf.example.com`); check on IP change | `dms._domainkey.example.org` TXT (note the wildcard CNAME) | `_dmarc.example.org` TXT (note the wildcard CNAME) | unchanged (external forwarder) |
| `example.net` | currently sufficient (VPS via `spf.example.com`); check on IP change | `dms._domainkey.example.net` TXT (note the wildcard CNAME) | `_dmarc.example.net` TXT (note the wildcard CNAME) | unchanged |
| `example.com` | currently sufficient (VPS via `a` and `spf.example.com`); check on IP change | `dms._domainkey.example.com` TXT | `_dmarc.example.com` exists: `v=DMARC1;p=reject;` | unchanged |
| `example.info` | currently sufficient (VPS via `spf.example.com`); check on IP change | `dms._domainkey.example.info` TXT (note the wildcard CNAME) | `_dmarc.example.info` TXT (note the wildcard CNAME) | unchanged |

### SPF

Rules:

- There must be **exactly one** SPF TXT record per domain.
- Do not remove existing mechanisms. Currently no change is needed because `spf.example.com` authorizes the VPS IP via `a:mx.example.com`.
- If the IP changes and `a:mx.example.com` no longer matches, add `ip4:<VPS_IP>`.
- Existing example:

```text
v=spf1 include:_spf.provider.example ~all
```

- New:

```text
v=spf1 include:_spf.provider.example ip4:<VPS_IP> ~all
```

- No existing SPF record:

```text
v=spf1 ip4:<VPS_IP> ~all
```

`~all` for the start; after successful validation it can be tightened to `-all`. Observe the 10-DNS-lookup limit; `ip4:` costs no lookup.

### DKIM

- Selector: `dms` (configured in `config/domains.yaml`).
- Name: `dms._domainkey.<domain>`
- Type: `TXT`
- Value: `v=DKIM1; k=rsa; p=<PUBLIC_KEY>`
- The key is generated separately per domain; all four domains need their own record.
- The public key is read out after the first pod start:

```bash
kubectl exec -n mail deploy/dms -- \
  cat /tmp/docker-mailserver/opendkim/keys/example.org/dms.txt
```

- Enter the content as a TXT record. With providers that do not accept parentheses/line breaks, join the value into one line.
- Check before deployment: does `dms._domainkey` already exist? If so, choose another selector (e.g. `dms2026`) and adjust `config/domains.yaml`.

### DMARC

Initial recommendation:

```text
v=DMARC1; p=none; sp=none; adkim=r; aspf=r; pct=100; fo=0; ri=86400; rua=mailto:dmarc@example.org
```

- `p=none` first, so misconfigurations become visible without discarding mail.
- After 1-2 weeks with passing DKIM/SPF tests, move to `p=quarantine` and later `p=reject`.
- The `rua` address must be reachable through the external forwarding.
- Do not blindly overwrite existing DMARC records; merge policy and reporting addresses.
- DMARC alignment:
  - `example.org` as the bounce domain: SPF aligned (envelope on `example.org`).
  - Other From domains: DKIM aligned (signature `d=<From domain>`), SPF is not aligned because of the envelope rewrite to `example.org`. DKIM is therefore mandatory for these domains.

### MX

- MX records stay unchanged.
- **No** MX is set on `mx.example.com`.
- `example.org` must remain reachable through the external forwarder, especially for `bounces@example.org`.
- The existing catch-all of `example.org` forwards `bounces@example.org` to the regular mailbox; no special rule is needed.

## 3. Domain-specific Notes

### example.org (most important domain)

- Some domain plans do not include mail sending, but DNS TXT/A/AAAA and external MX remain possible.
- Requirement: The DNS zone of `example.org` must be editable. If the provider does not allow the required TXT records in the plan, delegate the zone to another DNS provider (NS change) without changing the MX/forwarder.
- Our sending runs exclusively through the VPS IP.
- `bounces@example.org` is covered by the catch-all of the external forwarder.
- No MX on DMS.

### example.net

- Existing forwards and MX unchanged.
- Only add to SPF, do not replace it.

### example.com

- Technical auth account: `smtp@mail.example.com`.
- `mail.example.com` is a CNAME to `mx.example.com` and included in the TLS certificate; a separate A/MX record is not needed.
- `example.com` remains a remote domain for Postfix because only `mail.example.com` is in the account file. This routes mail to real `@example.com` recipients through the external MX.

### example.info

- Like the other sender domains; MX unchanged.

## 4. Verification Commands

```bash
dig +short A mx.example.com
dig +short -x <VPS_IP>
dig +short MX example.org
dig +short TXT example.org
dig +short TXT dms._domainkey.example.org
dig +short TXT _dmarc.example.org
```

Expectation:

- A points to `203.0.113.10`.
- PTR points to `mx.example.com` and the A record points back to `203.0.113.10`.
- MX still points to the external forwarder.
- SPF record authorizes `203.0.113.10` (currently via `include:spf.example.com` or `a`) and is the only SPF record.
- DKIM record starts with `v=DKIM1; k=rsa; p=`.
- DMARC record starts with `v=DMARC1;`.

## 5. External Validation

- SPF/DKIM/DMARC: mail to `check-auth@verifier.port25.com` or `dkimvalidator.com`.
- DKIM key: `opendkim-testkey -d <domain> -s dms -vv` (in the DMS container, if available).
- DMARC: `dmarcian.com/dmarc-inspector/` or `mxtoolbox.com`.
- Blocklists: `multirbl.valli.org`, `mxtoolbox.com/blacklists.aspx`.
- Overall score: `mail-tester.com` (only with a real test mail).
