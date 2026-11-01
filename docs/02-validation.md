# Phase 1 - Technical Validation

Date: 2026-10-31  
Reference: Docker Mailserver 16.x, Postfix 3.10, community Helm chart 6.0.0 (appVersion 16.0.1).

The table deliberately distinguishes:

- **confirmed**: evidenced by official documentation or current DMS/Postfix source code.
- **open**: the mechanics are documented but must be tested in the cluster test with exactly this combination.
- **not suitable**: contradicts the requirement.

This document does **not** claim that the overall configuration already runs. That is the task of Phase 2.

## 1. Validation Table

| No | Function / Assumption | Status | Evidence / Rationale | Consequence |
|---|---|---|---|---|
| 1 | `SMTP_ONLY=1` starts only Postfix | confirmed (docs) | DMS environment docs: "1 => only launch postfix smtp" | Not sufficient on its own |
| 2 | `SMTP_ONLY=1` + SMTP AUTH works | **not suitable** | `setup.d/postfix.sh`: with `SMTP_ONLY=1` and `ENABLE_SASLAUTHD=0`, `smtpd_sasl_auth_enable=no` is set; Dovecot is not started | Do not use `SMTP_ONLY` |
| 3 | Normal mode + `ENABLE_IMAP=0` + `ENABLE_POP3=0` keeps SMTP AUTH | confirmed (source) | `start-mailserver.sh` starts Dovecot when `SMTP_ONLY != 1`; `master.cf` enables SASL on `submission`/`submissions` | MVP configuration |
| 4 | SMTP AUTH on 465/587 via Dovecot SASL | confirmed (source) | `master.cf`: `smtpd_sasl_auth_enable=yes`, `smtpd_sasl_type=dovecot` | Runtime test open |
| 5 | OpenDKIM in SMTP-only mode | confirmed (source) | `start-mailserver.sh` starts OpenDKIM independently of `SMTP_ONLY` as long as `ENABLE_OPENDKIM=1` | We still use normal mode |
| 6 | DKIM signature per domain | confirmed (source) | `open-dkim`: `SigningTable` entry `*@<domain> <selector>._domainkey.<domain>`; `opendkim.conf` uses `SigningTable` | Runtime test open |
| 7 | DKIM key generation is idempotent | confirmed (source) | `open-dkim`: a key is only created when `<selector>.private` is missing; table entries are only appended | Init container suitable |
| 8 | `SPOOF_PROTECTION=1` allows arbitrary local parts | **not suitable** | `security/spoofing.sh`: enables `reject_authenticated_sender_login_mismatch`; `main.cf`: `mua_sender_restrictions` | `SPOOF_PROTECTION=0` |
| 9 | `SPOOF_PROTECTION=0` uses `$dms_smtpd_sender_restrictions` | confirmed (source) | `security/spoofing.sh`: sets `mua_sender_restrictions = $dms_smtpd_sender_restrictions` | Own restrictions possible |
| 10 | `check_sender_access` with a domain entry matches every local part | confirmed (docs) | `access(5)`: lookup order `user@domain`, `domain.tld`, `.domain.tld`, `user@` | Domain allowlist feasible |
| 11 | Trailing `reject` prevents foreign sender domains | confirmed (docs) | Postfix restriction lists work left to right; `reject` terminates | Runtime test open |
| 12 | Sender restrictions apply directly after `MAIL FROM` | **no, delayed** | `SMTPD_ACCESS_README`: `smtpd_delay_reject` evaluates sender restrictions only at `RCPT TO` | Rejection appears at RCPT; the envelope sender is still checked |
| 13 | `check_sender_access` + `reject` rejects the null sender | **open** | `access(5)`: null sender lookup uses `<>` as the key; behavior with `<> OK` not tested | Cluster test: `swaks --from ''` |
| 14 | From: header check via `header_checks` | confirmed (docs) | `header_checks(5)`: `if`/`!`/`endif`, logical headers, REJECT | Test regex coverage in the cluster test |
| 15 | DMS's own `sender-cleanup` header checks can be extended additively | open | `master.cf` sets `sender-cleanup/unix/header_checks=...`; override via `postfix-master.cf`/`postconf -P` | Cluster test: both maps must apply |
| 16 | A missing client `From:` stays unchecked | confirmed (docs) | `header_checks(5)`: headers added by `cleanup` are exempt | Clients must set From; DKIM/DMARC as backstop |
| 17 | Envelope rewrite to the bounce address | open | `sender_canonical_maps` + `sender_canonical_classes=envelope_sender` is Postfix standard; `canonical_maps` are recursive | Cluster test: check Return-Path, DKIM and the rewrite loop; use a negative lookahead |
| 18 | Bounce address on a non-local domain | confirmed (design) | DMS makes account domains `virtual_mailbox_domains`; `example.org` is not an account | `bounces@example.org` is routed remotely |
| 19 | Central account on the technical hostname domain prevents a local-domain collision | confirmed (design) | `postfix-accounts.cf` -> `virtual_mailbox_domains`; `mail.example.com` is not a real sender domain | `smtp@mail.example.com` |
| 20 | Queue lives in `/var/mail-state` | confirmed (source) | `mail_state.sh`: `spool/postfix` is moved to `/var/mail-state/spool-postfix` and symlinked | PVC for `state` mandatory |
| 21 | TLS 465 implicit / 587 STARTTLS | confirmed (source) | `master.cf`: `submissions` with `smtpd_tls_wrappermode=yes`; `submission` with `smtpd_tls_security_level=encrypt` | Test certificate and clients |
| 22 | DMS officially supports Kubernetes | **no** | DMS docs "Limited Support"; community Helm chart | Own manifest, no official guarantee |
| 23 | The community Helm chart fits outbound-only | **no, too heavy** | Chart 6.0.0: Rspamd on, OpenDKIM off, IMAP on, 4 PVCs, PROXY protocol on | Plain YAML for the MVP |
| 24 | k3s ServiceLB can do PROXY protocol | **no** | Chart README points to the k3s KlipperLB limitation | PROXY protocol off |
| 25 | Mount the account file from a Secret directly read-only | open / risky | DMS change detection and setup routines expect a writable config volume | Init container copies Secret -> PVC |
| 26 | Init container creates DKIM keys on the PVC | open | `opendkim-genkey` is idempotent but needs write access on the PVC | Cluster test |
| 27 | ConfigMap `subPath` updates a running pod | **no** | Kubernetes semantics: `subPath` mounts are not updated | Rollout after config change |
| 28 | NetworkPolicy is enforced by k3s Flannel | **no** | Flannel is the k3s default without policy enforcement; the target homelab repo names Flannel as the CNI | Policy is documentation only/Calico-Cilium migration; no protection guarantee |
| 29 | Outbound TCP 25 is allowed at the VPS | **yes (tested)** | From the VPS: `nc -4 -vz -w 5 mx00.gmx.net 25` -> `succeeded` (212.227.15.150), 2026-10-31 | Direct sending possible; test again when changing providers |
| 30 | PTR/A record match | **yes** | A = `203.0.113.10`, PTR = `mx.example.com`; FCrDNS consistent | On IP change, adjust the PTR at the hoster |
| 31 | MX for `mx.example.com` exists | **yes, but without a service** | MX `10 mx.example.com` (self-MX); TCP 25/465/587 closed from outside | No function for the MVP; only usable with inbound |
| 32 | DKIM/DMARC names are free of wildcard CNAMEs | **no** | `*.example.org`, `*.example.net`, `*.example.info` resolve `_dmarc`/`dms._domainkey` as CNAME | Create and verify explicit TXT records; remove the wildcard if needed |
| 33 | The SPF chain stays under 10 DNS query terms | **borderline** | `spf.example.com` has 7 terms; domains add their own `a`/`mx`/`include`, reaching up to 10 | Check with an SPF validator; simplify `spf.example.com` further if needed |


## 2. Sources

### DMS

- Environment variables: <https://docker-mailserver.github.io/docker-mailserver/latest/config/environment/>
- Kubernetes notes: <https://docker-mailserver.github.io/docker-mailserver/latest/config/advanced/kubernetes/>
- DKIM/SPF/DMARC: <https://docker-mailserver.github.io/docker-mailserver/latest/config/best-practices/dkim_dmarc_spf/>
- Source code:
  - `target/scripts/startup/setup.d/postfix.sh`
  - `target/scripts/startup/setup.d/security/spoofing.sh`
  - `target/scripts/startup/setup.d/dmarc_dkim_spf.sh`
  - `target/scripts/startup/setup.d/mail_state.sh`
  - `target/scripts/start-mailserver.sh`
  - `target/postfix/main.cf`
  - `target/postfix/master.cf`
  - `target/bin/open-dkim`

### Postfix

- `access(5)`: <https://www.postfix.org/access.5.html>
- `header_checks(5)`: <https://www.postfix.org/header_checks.5.html>
- `SMTPD_ACCESS_README`: <https://www.postfix.org/SMTPD_ACCESS_README.html>
- `postconf(5)`: <https://www.postfix.org/postconf.5.html>

### Helm Chart

- <https://github.com/docker-mailserver/docker-mailserver-helm>
- Chart `6.0.0`, `appVersion: 16.0.1`

## 3. Acceptance Tests (Kubernetes Cluster)

Test domains: `example.org` and `example.net`.

Account: `smtp@mail.example.com` (technical subdomain so no real sender domain becomes local).

Bounce: `bounces@example.org` (not local).

Note: The tests assume a trusted certificate for `mx.example.com`. Self-signed is not a production state for SMTP submission. `openssl s_client` is suitable for the handshake test.

### T1 - No auth

```bash
swaks --server mail.example.com --port 587 --tls \
  --from test@example.org --to recipient@example.net
```

Expectation: rejection due to missing authentication (5xx, "Authentication required").

### T2 - Auth + allowed domain + arbitrary local part

```bash
swaks --server mail.example.com --port 587 --tls \
  --auth-user smtp@mail.example.com --auth-password '<PASSWORD>' \
  --from typo@example.org --to recipient@example.net
```

Expectation: `250` after DATA, queue ID in the log.

### T3 - Auth + disallowed domain

```bash
swaks --server mail.example.com --port 587 --tls \
  --auth-user smtp@mail.example.com --auth-password '<PASSWORD>' \
  --from hans@gmail.com --to recipient@example.net
```

Expectation: rejection at `RCPT TO` with 5xx (domain not allowed).

### T4 - Allowed envelope, foreign From header

```bash
swaks --server mail.example.com --port 587 --tls \
  --auth-user smtp@mail.example.com --auth-password '<PASSWORD>' \
  --from test@example.org \
  --header 'From: hans@gmail.com' \
  --to recipient@example.net
```

Expectation: rejection at DATA (header check).

### T5 - Allowed envelope + allowed From header

```bash
swaks --server mail.example.com --port 587 --tls \
  --auth-user smtp@mail.example.com --auth-password '<PASSWORD>' \
  --from test@example.org \
  --header 'From: Any Name <typo@example.net>' \
  --to recipient@example.net
```

Expectation: `250`, DKIM signature with `d=example.net`, `s=dms`.

### T6 - Null sender

```bash
swaks --server mail.example.com --port 587 --tls \
  --auth-user smtp@mail.example.com --auth-password '<PASSWORD>' \
  --from '' --to recipient@example.net
```

Expectation: if `<> OK` applies correctly, accept; otherwise fix it specifically. The result decides whether the map line `<> OK` stays.

### T7 - TLS

- Port 465: implicit TLS, check the certificate against `mx.example.com`.
- Port 587: STARTTLS, plaintext submission must be refused.

```bash
openssl s_client -connect mx.example.com:465 -servername mx.example.com -brief
openssl s_client -connect mx.example.com:587 -servername mx.example.com -starttls smtp -brief
```

### T8 - DKIM

- Send mail to a verifier (e.g. `check-auth@verifier.port25.com` or `dkimvalidator.com`).
- Alternatively: evaluate the `Authentication-Results` of a test mailbox.
- Expectation: `dkim=pass` with `d=<From domain>` and `s=dms`.
- Set the DNS record first: `dms._domainkey.<domain>`.

### T9 - Queue and retry

- Recipient MX temporarily unreachable: `status=deferred` with 4xx.
- `postqueue -p` shows the queue ID; `postcat -q <ID>` shows the content.
- After reachability: `status=sent` with 250 of the target server.
- Permanent 5xx: `status=bounced`, DSN to the bounce address.

### T10 - Bounce delivery

- Send mail to a non-existent recipient address.
- Expectation: DSN to `bounces@example.org`; external forwarding delivers it.
- Check: `Return-Path`, DSN headers, queue ID, error code.
- Also check that `bounces@example.org` is not rewritten again (no `mail loops back to myself` or rewrite errors in the log).

### T11 - Domain change

- Add a new domain to `config/domains.yaml`, regenerate the maps, restart the pod.
- Expectation: sending as the new domain is allowed; DKIM key exists; DNS record documented.
- Removed domain: sending is rejected; the DKIM key initially remains (clean up later).

### T12 - Header edge cases

- Display name containing `@` and a foreign address.
- Multiple `From:` headers.
- Comment after the address.
- Missing `From:` header.

Expectation: documented behavior; no false claim of a hard guarantee. The envelope allowlist and DKIM/DMARC remain the robust layers.

## 4. Abort Criteria

Phase 2 is considered failed if:

- SMTP AUTH on 465/587 does not work.
- A disallowed envelope domain is accepted.
- An allowed domain with an arbitrary local part is rejected.
- DKIM does not sign with the From domain.
- The queue does not survive a pod restart.
- Bounces do not arrive at the configured bounce address.
- TLS certificate and hostname do not match.
