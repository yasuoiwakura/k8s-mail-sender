# Architecture and Design Decisions (Phase 1)

Date: 2026-10-31  
Status: **Design and Kubernetes manifests implemented; cluster validation pending**

Goal: a lean, secure SMTP outbound relay based on Docker Mailserver (DMS) in k3s, managed by ArgoCD.

## 1. Goals and Non-goals

### Goals (MVP)

- Send outgoing email via SMTP submission with SMTP AUTH.
- Exactly one central SMTP account.
- This account may send as any address within the four configured domains.
- The local part is arbitrary; there are no sender lists, aliases or mailboxes per sender.
- Sender domains that are not allowed are rejected.
- No open relay.
- DKIM signature per domain, SPF/DMARC compliant delivery.
- Queue and configuration survive pod restarts.
- Traceable bounce/DSN and queue handling.
- Declarative, domain-centric configuration without secrets in Git.

### Non-goals (MVP)

- No inbound SMTP traffic on port 25.
- No MX changes.
- No IMAP/POP3, no mailboxes for end users.
- No spam filter, virus scan or Amavis pipeline.
- No customer domains, no per-domain accounts, no Sieve, no complex routing.
- No HA/multi-replica operation.

These points remain extendable later but do not force an architecture now.

## 2. Architecture Overview

```text
Thunderbird / FairEmail / Gitea / Authentik / Grafana / Uptime Kuma
        |
        |  465 implicit TLS  or  587 STARTTLS
        |  SMTP AUTH (one central account)
        v
k3s Service "dms" (LoadBalancer, ONLY 465/587)
        |
        v
Pod "dms" (Docker Mailserver 16.x)
  Postfix submission/submissions
    -> SMTP AUTH via Dovecot SASL
    -> Envelope sender allowlist (check_sender_access)
    -> From: header check (header_checks, defense in depth)
    -> Envelope rewrite to bounces@example.org
    -> OpenDKIM signature with the key table of the From domain
    -> local Postfix queue (PVC)
    -> SMTP client directly to the recipient MX on TCP 25
        |
        v
Internet
```

Important:

- Port 25 is **not** published externally. The pod may speak outbound TCP 25.
- The external MX/forwarding operation of the domains stays unchanged.
- There is no HTTP ingress for SMTP. Traefik is not needed for the MVP.
- A later Rspamd web UI would be the only candidate for Traefik; Rspamd is off in the MVP.

## 3. Assessment of Docker Mailserver

### Arguments for DMS

- Postfix, Dovecot SASL and OpenDKIM are already integrated and tested.
- DKIM key generation and DNS TXT output are built in (`setup config dkim`; in the manifest the init container calls `opendkim-genkey` directly).
- Account management and password hashes are built in.
- Active project, clear documentation, regular releases.
- One central container instead of a self-assembled Postfix/Dovecot/OpenDKIM combination.
- Sufficient for the MVP; later inbound/IMAP expansion is possible.

### Arguments against DMS

- DMS is a full mail server, not a pure outbound relay. Dovecot also runs without IMAP/POP3 (for SASL auth and LMTP).
- Kubernetes is **not officially** supported; community support only.
- The container runs as root and requires capabilities; PodSecurity `restricted` is not possible.
- The bundled domain allowlist does not exist; it must be built via Postfix overrides.
- The community Helm chart is too heavy for the outbound-only case (see section 8).
- `SMTP_ONLY=1` is a trap for this use case (see section 4).

### Decision

DMS remains the best KISS compromise, **but not in SMTP_ONLY mode**. We run DMS in normal mode with IMAP/POP3 disabled. Dovecot stays active only for SASL authentication. That is the decisive difference from a naive `SMTP_ONLY=1` setup.

## 4. The SMTP_ONLY Pitfall (most important finding)

The DMS documentation describes `SMTP_ONLY=1` as "only launch postfix smtp". That sounds exactly like the desired outbound setup, but it is incompatible with SMTP AUTH:

- With `SMTP_ONLY=1`, DMS does not start Dovecot.
- Postfix authenticates against DMS via Dovecot SASL (`smtpd_sasl_type = dovecot`).
- With `SMTP_ONLY=1` and `ENABLE_SASLAUTHD=0`, DMS explicitly disables SASL in `main.cf` and in the submission services of `master.cf`.
- Result: no SMTP AUTH, therefore no sending for Thunderbird/FairEmail/apps.
- OpenDKIM itself still starts with `SMTP_ONLY=1` (if enabled), but without SMTP AUTH the combination is still unusable for this use case.

Sources (source code, not just docs):

- `target/scripts/startup/setup.d/postfix.sh`: with `SMTP_ONLY=1` and `ENABLE_SASLAUTHD=0` it sets `smtpd_sasl_auth_enable = no`.
- `target/scripts/start-mailserver.sh`: starts Dovecot only when `SMTP_ONLY != 1`.
- `target/postfix/master.cf`: `submission`/`submissions` explicitly enable SASL with `smtpd_sasl_type=dovecot`.

### Consequence

```text
SMTP_ONLY=1                    -> no SMTP AUTH, unusable for us
do not set SMTP_ONLY
ENABLE_IMAP=0
ENABLE_POP3=0                  -> Dovecot runs only for SASL/LMTP, no IMAP/POP3 listeners
```

This is the central technical decision. It must be explicitly tested in the cluster test.

## 5. Domain-based Sender Authorization

### 5.1 Single Source of Truth

`config/domains.yaml` is the only source for sender domains. The following is generated from it:

- `sender_domains`: Postfix access map for the envelope sender.
- `from_domains.pcre`: Postfix header check for the visible `From:` header.
- `domains.txt`: list for DKIM key generation in the init container.
- `manifests/10-configmap-env.yaml` and `manifests/11-configmap-files.yaml`.
- The DNS checklist stays manual in `docs/03-dns.md`; the DKIM public keys are read out after deployment.

**No** individual sender addresses are maintained.

### 5.2 Envelope Sender (MAIL FROM)

Postfix access map, e.g. `sender_domains`:

```text
example.org OK
example.net OK
example.com OK
example.info OK
<> OK
```

`postfix-main.cf`:

```text
dms_smtpd_sender_restrictions = check_sender_access texthash:/tmp/docker-mailserver/sender_domains, reject
```

Effect:

- `check_sender_access` first looks up the full address, then the domain, then parent domains.
- An entry `example.org OK` therefore matches **every** local part under `example.org`.
- The trailing `reject` rejects any domain that is not listed.
- `<> OK` is intended for the null sender (DSNs/bounces); this must be verified in the cluster test. If the map parser does not accept `<>`, `smtpd_null_access_lookup_key` can be set to a simple ASCII key and that key stored in the map.
- `texthash:` does not need `postmap`; the file can be mounted directly.

Important regarding semantics:

- Because of `smtpd_delay_reject=yes`, Postfix evaluates `smtpd_sender_restrictions` only at `RCPT TO`, not directly after `MAIL FROM`.
- The envelope sender from `MAIL FROM` is still checked. The client only sees the rejection later.
- Rejecting directly after `MAIL FROM` would require a policy daemon. That is not proportionate for the MVP.

### 5.3 Visible From: Header

`postfix-master.cf` extends the `sender-cleanup` service of the submission ports:

```text
sender-cleanup/unix/header_checks=pcre:/etc/postfix/maps/sender_header_filter.pcre, pcre:/tmp/docker-mailserver/from_domains.pcre
```

`from_domains.pcre` (generated):

```text
if /^From:/
!/^From:.*@(?:example\.org|example\.net|example\.com|example\.info)[ \t]*>?[ \t]*$/i REJECT 5.7.1 Sender domain not allowed
endif
```

This keeps the DMS-specific privacy header rules and adds the domain check on top.

Limits of this solution (deliberately accepted, documented):

- `header_checks` work line by line on logical headers; exotic comments/encodings can bypass the regex.
- A `From:` header not set by the client at all is added by Postfix itself and is exempt from `header_checks`.
- The **robust** protection is therefore two-layered:
  1. The envelope allowlist is authoritative.
  2. DKIM signs only domains from the key table; an unauthorized From domain remains unsigned and fails DMARC.
- Anyone who needs a hard, parser-based From check cannot avoid a small policy daemon. That is not MVP.

### 5.4 SPOOF_PROTECTION

`SPOOF_PROTECTION=1` is **not** suitable:

- It enables `reject_authenticated_sender_login_mismatch`.
- This means the account may only send with its own address or its aliases.
- That is exactly what we do not want: an arbitrary local part in all own domains.

With `SPOOF_PROTECTION=0`, DMS sets:

```text
mua_sender_restrictions = $dms_smtpd_sender_restrictions
```

So our domain allowlist applies without the login mismatch check. Sources: `target/postfix/main.cf`, `target/scripts/startup/setup.d/security/spoofing.sh`.

## 6. Central Account and the Local-Domain Trap

DMS treats every domain from `postfix-accounts.cf` as a **local** domain (`virtual_mailbox_domains`). That has an unpleasant consequence:

- With `smtp@example.com`, `example.com` would be local.
- Outbound mail to real recipients `@example.com` (which are forwarded externally) would then not be delivered via MX, but handed to Dovecot locally and fail.
- A bounce address `bounces@example.com` would also be local and would not be forwarded externally.

Therefore:

```text
Central auth account: smtp@mail.example.com
Bounce address:       bounces@example.org
```

- `mail.example.com` is the technical hostname/account domain, via CNAME to `mx.example.com` and included in the TLS certificate; it is not used as a real sender domain.
- None of the four sender domains is local because of this.
- `bounces@example.org` is delivered through the existing external MX/forwarder of `example.org`.
- The existing catch-all of `example.org` covers `bounces@example.org`; no special forwarding rule is needed. The MX records stay unchanged.

This is a deliberate, small deviation from "one account @ example.com", but it prevents a real delivery failure.

## 7. Bounce and DSN Concept

### Envelope Rewrite

`postfix-main.cf`:

```text
sender_canonical_maps = pcre:/tmp/docker-mailserver/sender_canonical.pcre
sender_canonical_classes = envelope_sender
```

`sender_canonical.pcre` (generated, one rule):

```text
/^(?!bounces@example\.org$).+@.+/i bounces@example.org
```

Effect:

- The visible `From:` header stays unchanged (`canonical_classes=envelope_sender`).
- The envelope sender (Return-Path) is set to `bounces@example.org`.
- All DSNs/bounces reliably reach a reachable address.
- SPF is checked for `example.org`; the VPS IP must be listed there.
- DKIM continues to sign the From domain. For From domains other than `example.org`, DKIM is the aligning method for DMARC, not SPF.
- The negative lookahead rule prevents an infinite loop because `canonical_maps` are applied recursively: `bounces@example.org` does not match again and is not rewritten again.

Trade-off:

- Without the rewrite, bounces would be addressed to arbitrary typo addresses of the sender domains and only reachable with a catch-all.
- With the rewrite, DKIM must be correct for all non-`example.org` domains. That is a hard requirement anyway.

### What 250 Means

- `250` at submission: our Postfix accepted the mail and put it in the queue.
- `250` on outbound SMTP: the next MX accepted the mail.
- This is **not** a guarantee that the mail is in the inbox. Only a DSN or a later recipient confirmation gives a definitive answer.

### Queue and Logs

- Show queue: `postqueue -p` or `mailq`
- Inspect queue ID: `postcat -q <QUEUE_ID>`
- Retry: `postsuper -r <QUEUE_ID>`
- Delete: `postsuper -d <QUEUE_ID>`
- Logs in the pod: `/var/log/mail/mail.log`, plus `kubectl logs`
- Typical status values: `status=sent`, `status=deferred`, `status=bounced`
- Retry: 4xx = temporary, 5xx = permanent; default lifetime 5 days (`maximal_queue_lifetime`).

The MVP has no additional exporter. A `postfix-exporter`/CronJob for queue thresholds is a later option.

## 8. Kubernetes and GitOps

### Decision: vanilla YAML, no Helm/Kustomize for the MVP

Rationale:

- One namespace, one Deployment, one PVC, one Service, two ConfigMaps, two Secrets. Templating adds no value for this.
- Full control over Postfix maps and mounts without chart abstraction.
- ArgoCD can sync a simple directory of YAML.
- CKA learning value: Deployment, Service, ConfigMap, Secret, PVC, probe, SecurityContext, NetworkPolicy.
- Kustomize/Helm can be added later for overlays or distributions without invalidating the YAML.

### Workload

- `Deployment`, `replicas: 1`, `strategy: Recreate`.
- Image pinned, e.g. `mailserver/docker-mailserver:16.0.1`; no `latest`. Updates are tested deliberately.
- No StatefulSet in the MVP: no stable pod identity, no ordinality, no multi-writer. One PVC and one writer are sufficient.
- `Recreate` is mandatory because of the RWO PVC; RollingUpdate would hang on the old pod.
- For later HA operation, the decision must be re-evaluated (StatefulSet/leader, queue behavior).

### DMS Environment (MVP)

| Variable | Value | Reason |
|---|---|---|
| `OVERRIDE_HOSTNAME` | `mx.example.com` | HELO, banner, PTR match |
| `SMTP_ONLY` | **do not set** | otherwise no SMTP AUTH |
| `ENABLE_IMAP` | `0` | no IMAP listener |
| `ENABLE_POP3` | `0` | no POP3 listener |
| `ENABLE_OPENDKIM` | `1` | DKIM signature per domain |
| `ENABLE_OPENDMARC` | `0` | no inbound DMARC check needed |
| `ENABLE_POLICYD_SPF` | `0` | no inbound SPF check needed |
| `ENABLE_RSPAMD` | `0` | no additional suite |
| `ENABLE_AMAVIS` | `0` | no content filter |
| `ENABLE_CLAMAV` | `0` | no virus scan |
| `ENABLE_SPAMASSASSIN` | `0` | no spam filter |
| `ENABLE_POSTGREY` | `0` | no greylisting |
| `ENABLE_FAIL2BAN` | `0` | MVP; evaluate separately later |
| `ENABLE_DNSBL` | `0` | no inbound postscreen |
| `ENABLE_SRS` | `0` | SRS would override `sender_canonical_maps` |
| `ENABLE_UPDATE_CHECK` | `0` | no unwanted reports |
| `ENABLE_QUOTAS` | `0` | no mailboxes |
| `PERMIT_DOCKER` | `none` | enforce auth, no open relay |
| `SPOOF_PROTECTION` | `0` | domain allowlist instead of login mismatch |
| `SSL_TYPE` | `manual` | certificate from Secret |
| `TLS_LEVEL` | `modern` | TLS >= 1.2 |
| `POSTFIX_INET_PROTOCOLS` | `ipv4` | consistent with SPF/PTR |
| `DOVECOT_INET_PROTOCOLS` | `ipv4` | consistent |
| `POSTMASTER_ADDRESS` | `bounces@example.org` | reachable bounce address |
| `TZ` | `Europe/Berlin` | logs/queue times |
| `LOG_LEVEL` | `info` | operational logs |

### Persistence

One PVC (k3s `local-path`, about 5 GiB) with subPaths:

```text
/tmp/docker-mailserver   -> config
/var/mail-state          -> state   (contains spool/postfix = queue)
/var/mail                -> mail
/var/log/mail            -> log
```

`/var/mail-state` is critical for the queue: DMS moves `spool/postfix` there and symlinks it at startup. Without this volume the queue is lost when the pod is replaced.

`local-path` is node-local. In a multi-node cluster the pod must stay on the node or shared storage (Longhorn/NFS) must be used. The MVP assumes a single-node k3s; this is to be verified.

### Service

- `type: LoadBalancer` (k3s ServiceLB), ports **only** `465` and `587`.
- No port 25 in the Service.
- `externalTrafficPolicy: Local` to preserve the client IP (uncritical on a single node, useful later for Fail2Ban/logs).
- Internal access for Gitea/Authentik/Grafana/Uptime Kuma via the same Service (ClusterIP) on port 587 with STARTTLS.
- Port 25 also stays closed at the firewall/NetworkPolicy level. The pod still listens internally, but there is no Service port and no external path to it.
- If clients use the internal DNS name `dms.mail.svc.cluster.local`, the TLS certificate does not match. Recommendation: use `mx.example.com:587` internally or provide a certificate with a matching SAN.

### TLS

- `SSL_TYPE=manual`, certificate and key as a Kubernetes Secret.
- Preferred: extract the LE certificate from Traefik `acme.json` (`scripts/extract-traefik-cert.py`), or cert-manager/wildcard/manual. Details: `docs/06-traefik-tls.md`.
- Self-signed is not a production state for SMTP submission.
- The certificate must contain `mx.example.com` and/or `mail.example.com` (wildcard `*.example.com` covers both).
- Port 465: implicit TLS. Port 587: STARTTLS, enforced by DMS.

### Secrets

- `dms-account`: `postfix-accounts.cf` with the password hash of the one account.
- `dms-tls`: `tls.crt`/`tls.key`.
- DKIM private keys: generated by the init container on the PVC, not in Git. Back up separately.
- No plaintext passwords, no private keys in the repository.
- The account file is copied by the init container from the Secret into the writable config PVC. A direct read-only mount can collide with DMS change detection and is not used as the default in the cluster deploy.

### ConfigMaps

- `dms-env`: non-sensitive environment variables.
- `dms-files`: `postfix-main.cf`, `postfix-master.cf`, `sender_domains`, `from_domains.pcre`, `sender_canonical.pcre`, `domains.txt`.
- ConfigMap files are mounted via `subPath`. Changes to ConfigMaps are **not** propagated to running pods with `subPath` mounts. After domain/config changes a rollout is needed (`kubectl rollout restart deployment/dms -n mail`) or later a reloader.

### InitContainer for DKIM

- Starts with the DMS image.
- Copies the account file from the Secret into the config PVC.
- Reads `domains.txt` and calls `opendkim-genkey` with selector `dms` per domain.
- Creates missing keys idempotently and does not overwrite existing keys.
- Writes `KeyTable`, `SigningTable` and `TrustedHosts` to `/tmp/docker-mailserver/opendkim`.
- Keys land under `/tmp/docker-mailserver/opendkim/keys/<domain>/dms.private` on the PVC.
- On the first start the public keys are output for DNS; afterwards they are stable.
- Alternatively keys could be managed as a Secret; for the MVP PVC + init container is simpler.

### Pod Security

DMS requires root and capabilities (CHOWN, SETUID, SETGID, NET_BIND_SERVICE, NET_ADMIN/NET_RAW only if Fail2Ban, etc.). A namespace with `pod-security.kubernetes.io/enforce=restricted` does not work. `baseline` is realistic; the exact capabilities are taken from the DMS Kubernetes docs.

### NetworkPolicy

- Ingress: only 465/587 from the allowed namespaces.
- Egress: DNS (53), TCP 25 to arbitrary destinations, optionally 443 for updates (disabled in the MVP).
- Note: k3s uses Flannel by default; the target homelab repo also names Flannel as the CNI. Flannel does not enforce NetworkPolicies. The policy is therefore documentation in the MVP or for a later Calico/Cilium migration, not a protection guarantee.

### ArgoCD

- Recommendation: one directory `manifests/` with plain YAML.
- The `Application` can live in this repo or in the parent homelab repo.
- For integration into `example-homelab-cluster`: create an Application there with `source.repoURL` pointing to this repo and `source.path: manifests`. No submodule requirement, no Helm repo needed.
- `syncPolicy.automated` with `prune` and `selfHeal`; protect the PVC with `argocd.argoproj.io/sync-options: Delete=false`.
- Secrets are created out-of-band (or later SOPS/SealedSecrets). ArgoCD does not manage them in the MVP.
- The homelab repo uses Kustomize with `base/` and `overlays/`. It can later include this repo as a remote base or point an Application directly at `manifests/`. No separate Kustomize structure is needed here for that; optionally a minimal `kustomization.yaml` can be added later as an integration point.

### Why Not the Community Helm Chart?

The chart `docker-mailserver/docker-mailserver-helm` (v6.0.0, appVersion 16.0.1) is maintained, but:

- Rspamd is on by default, OpenDKIM/DMARC/SPF are off, IMAP is on.
- Four PVCs, PROXY protocol on by default, resource defaults 1 CPU/1.5 GiB.
- PROXY protocol cannot be used with k3s ServiceLB according to the chart docs.
- For outbound-only, many defaults would have to be overridden.
- Additional chart abstraction (configMaps/secrets/initContainers) for two Postfix files.
- Community support, no official DMS Kubernetes support.

The chart remains a good fallback option when maintenance/upgrades become more important than transparency. For the MVP, plain YAML is leaner.

## 9. DNS Concept

Details and checklists: `docs/03-dns.md`.

Summary:

- `A mx.example.com -> <VPS_IP>` (one host for all domains).
- `PTR 203.0.113.10 -> mx.example.com` is set; Forward-Confirmed Reverse DNS is consistent. On an IP change the PTR must be updated at the VPS provider.
- SPF per domain: do not replace the existing `v=spf1`. Currently the VPS IP is already covered via `a:mx.example.com` in `spf.example.com` or via the `a` mechanism of `example.com`. This must be checked again after IP changes. No second SPF record.
- DKIM per domain: `dms._domainkey.<domain>` TXT with the public key.
- DMARC per domain: `_dmarc.<domain>` TXT, start with `p=none`, tighten later.
- The MX records of the sender domains stay unchanged (external forwarder).
- A self-MX (`10 mx.example.com`) already exists on `mx.example.com`, but currently there is no SMTP service on TCP 25. The record stays unchanged and unused for the MVP; it is only used with inbound expansion.
- Note: `*.example.org`, `*.example.net` and `*.example.info` are wildcard CNAMEs. As a result, `_dmarc` and `dms._domainkey` currently resolve as CNAMEs. Explicit TXT records must be created and verified there before DKIM/DMARC.
- `example.org`: the domain plan does not prevent a DNS TXT/A record or an external MX; only sending through the registrar's own mail service is not included. Sending goes through the VPS IP.
- IPv6 off in the MVP (`inet_protocols=ipv4`), otherwise AAAA, PTR and SPF would have to be consistent.

## 10. Proposed Repository Structure

```text
k8s-mail-infra/
|-- README.md
|-- config/
|   `-- domains.yaml              # single source of truth
|-- scripts/
|   |-- generate.py               # domains.yaml -> ConfigMaps
|   |-- bootstrap-secrets.sh      # dms-account, optional dms-tls
|   `-- extract-traefik-cert.py   # acme.json -> dms-tls/tls.crt|key
|-- manifests/
|   |-- 00-namespace.yaml
|   |-- 10-configmap-env.yaml     # generated
|   |-- 11-configmap-files.yaml   # generated
|   |-- 20-pvc.yaml
|   |-- 30-deployment.yaml
|   `-- 40-service.yaml
|-- argocd/
|   `-- application.yaml          # template; adjust repoURL
|-- docs/
|   |-- 01-architecture.md
|   |-- 02-validation.md
|   |-- 03-dns.md
|   |-- 04-checklist.md
|   |-- 05-bootstrap.md
|   |-- 06-traefik-tls.md
|   `-- 07-mail-clients.md
`-- .gitignore
```

Deliberately not in the MVP:

- `helm/` or `kustomize/`: no added value for a single Deployment.
- `charts/`: only once the chart is actually used.
- `docker-compose/`: omitted because deployment is directly to Kubernetes.
- No database, no Redis, no Rspamd, no ClamAV.

## 11. Technical Pitfalls

### Critical

1. `SMTP_ONLY=1` disables SMTP AUTH. Do not use.
2. `SPOOF_PROTECTION=1` blocks arbitrary local parts. Set to `0` and replace with the domain allowlist.
3. The account domain becomes local. Therefore `smtp@mail.example.com`, not `smtp@example.com`.
4. The bounce address must not be on a local DMS domain. Therefore `bounces@example.org`.
5. Outbound TCP 25 may be blocked at the VPS provider. Successfully tested from the VPS on 2026-10-31 (GMX MX, TCP 25 reachable). Check again when changing providers; otherwise a relayhost is required.
6. A missing/wrong PTR leads to rejections and spam classification. Currently the PTR is correct; check again on an IP change.
7. DKIM key gone + DNS record remains = DKIM fail. Document PVC backup and key rotation.
8. DMARC `p=reject` plus envelope rewrite: DKIM must be correct for all non-bounce domains.

### Important

9. Merge the SPF record, do not overwrite it; at most one SPF record per domain; observe the 10-DNS-lookup limit.
10. Do not point the MX records of the sender domains at DMS; there is no inbound SMTP. The existing self-MX on `mx.example.com` has no service in the MVP and remains unused.
11. The TLS certificate must contain `mx.example.com`; clients check the name.
12. Port 465 is implicit TLS, port 587 is STARTTLS. Clients must not confuse them.
13. Internal apps that use `dms.mail.svc.cluster.local` see a certificate for `mx.example.com`. Either use the public name or provide a SAN.
14. `header_checks` are not a hard parser. Envelope allowlist + DKIM/DMARC are the robust layers.
15. A missing `From:` header is added by Postfix and not checked. Clients must set a valid From.
16. `subPath` ConfigMaps do not update running pods. Roll out after a config change.
17. RWO PVC + RollingUpdate blocks. Use `Recreate`.
18. `local-path` is node-local; pod rescheduling to another node loses queue/config.
19. k3s Flannel does not enforce NetworkPolicies. Check the CNI.
20. DMS runs as root; `restricted` PodSecurity fails.
21. New IP without reputation: start slowly, watch blocklists, use IP warmup if needed.
22. IPv6: AAAA/PTR/SPF would otherwise be inconsistent; IPv4 only in the MVP.
23. Null sender (`<>`) and `check_sender_access` must be tested in the cluster test.
24. Read-only mounted `postfix-accounts.cf` and DMS change detection: test the behavior.
25. `postconf -P` override for `sender-cleanup/header_checks` with two maps: verify in the cluster test.
26. InitContainer DKIM generation with PVC permissions: verify in the cluster test.
27. `canonical_maps` are recursive. A catch-all rule without a negative lookahead on the target address can create a rewrite loop.
28. `ENABLE_SRS=1` sets `sender_canonical_maps` to postsrsd and would override the bounce rewrite. Leave it off in the MVP.
29. Internal apps must use SMTP submission (465/587) with auth. Mail via the local `sendmail` binary bypasses the SMTP sender/header checks.

## 12. Open Points Before Production

Already decided:

- Auth account `smtp@mail.example.com`.
- Bounce address `bounces@example.org`; the catch-all covers it.
- DKIM selector `dms`.
- Outbound TCP 25 is tested and allowed.
- PTR is set and consistent.

Still to clarify:

1. TLS: extract the certificate from Traefik `acme.json`, cert-manager, or use an existing wildcard certificate?
2. Storage: confirm single-node k3s with `local-path`; otherwise clarify StorageClass/node pinning.
3. DMARC rollout: start with `p=none` for the domains without an existing record; `example.com` initially stays at `p=reject`.
4. Network: Calico/Cilium present (NetworkPolicy effective) or Flannel (policy documentation only)?
5. Create explicit DKIM/DMARC TXT records despite the wildcard CNAMEs.
6. Check the SPF lookup limit with a validator.

Next step: cluster deployment with the acceptance tests from `docs/02-validation.md` and the bootstrap guide in `docs/05-bootstrap.md`.
