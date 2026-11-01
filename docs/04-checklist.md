# KISS Checklists: Deployment and Server Migration

Date: 2026-10-31  
These checklists are deliberately short and actionable. Details are in `01-architecture.md`, `02-validation.md` and `03-dns.md`.

## 0. Before Every Action

- [ ] `config/domains.yaml` is the only source for sender domains.
- [ ] The server/HELO hostname is `mx.example.com`; mail clients use `mail.example.com`.
- [ ] The auth account is `smtp@mail.example.com`.
- [ ] The bounce address is `bounces@example.org`.
- [ ] The DKIM selector is `dms`.
- [ ] No secrets, private keys or passwords in Git.
- [ ] A backup of the current state exists.
- [ ] Changes are documented traceably in the repo.

## 1. Deployment Checklist (Initial Setup)

### 1.1 DNS Before Deployment

- [ ] `A mx.example.com -> 203.0.113.10` is set.
- [ ] `PTR 203.0.113.10 -> mx.example.com` is set and FCrDNS is consistent.
- [x] Outbound TCP 25 tested from the VPS: successful (GMX MX, 2026-10-31).
- [ ] MX records of the sender domains are unchanged.
- [ ] The existing self-MX `mx.example.com -> mx.example.com` is known; no SMTP service runs there in the MVP. The record deliberately remains as a placeholder for inbound.
- [ ] SPF of all four domains authorizes the VPS IP (currently via `spf.example.com` / `a`).
- [ ] SPF checked with a validator; lookup limit respected.
- [ ] The DKIM selector `dms` is not already in use for any domain.
- [ ] **Explicit TXT records** are created for `dms._domainkey.<domain>`, not just wildcard CNAMEs.
- [ ] Explicit TXT records are created for `_dmarc.<domain>`.
- [ ] `example.com` has `p=reject`; the other domains deliberately start with `p=none`.
- [ ] The CAA record allows the chosen CA, if CAA exists.
- [ ] Wildcard CNAMEs (`*.example.org`, `*.example.net`, `*.example.info`) no longer hide required DKIM/DMARC names.

### 1.2 Secrets and Preparation

- [ ] `python scripts/generate.py --check` is clean.
- [ ] Traefik `defaultGeneratedCert` configured for `mx.example.com` (plus `mail.example.com` if needed).
- [ ] All SANs in the Traefik certificate have valid A/AAAA records.
- [ ] LE certificate for `mx.example.com` / `mail.example.com` checked externally with `openssl s_client`.
- [ ] `dms-tls` extracted from `acme.json` (`extract-traefik-cert.py --secret`).
- [ ] Password hash for `smtp@mail.example.com` created (e.g. SHA512-CRYPT).
- [ ] `postfix-accounts.cf` present as a Kubernetes Secret (`scripts/bootstrap-secrets.sh`).
- [ ] TLS certificate and key present as a Kubernetes Secret (`extract-traefik-cert.py --secret` or `bootstrap-secrets.sh`).
- [ ] Certificate contains `mx.example.com` and `mail.example.com`.
- [ ] DKIM private keys are not in Git; the backup/restore path is clear.
- [ ] ArgoCD repository and target path (`manifests/`) are defined.
- [ ] Namespace `mail` is allowed in the target cluster.

### 1.3 Kubernetes

- [ ] PVC with `local-path` or a defined StorageClass created.
- [ ] SubPaths for `config`, `state`, `mail`, `log` present.
- [ ] ConfigMap `dms-env` contains the MVP environment.
- [ ] ConfigMap `dms-files` contains `postfix-main.cf`, `postfix-master.cf`, `sender_domains`, `from_domains.pcre`, `sender_canonical.pcre`.
- [ ] Deployment: `replicas: 1`, `strategy: Recreate`, pinned image.
- [ ] Service publishes **only** 465 and 587, not 25.
- [ ] Service external IP matches `203.0.113.10` or the A record of `mx.example.com`.
- [ ] SecurityContext matches the DMS requirements (root, capabilities).
- [ ] Readiness/liveness probe configured.
- [ ] NetworkPolicy documented; with Flannel it is not enforced.
- [ ] After ConfigMap changes a rollout is planned (`kubectl rollout restart deployment/dms -n mail`).
- [ ] ArgoCD Application syncs successfully.
- [ ] Pod is running; logs show no startup errors.

### 1.4 DKIM

- [ ] Init container created DKIM keys for all four domains.
- [ ] Keys are under `/tmp/docker-mailserver/opendkim/keys/<domain>/dms.private`.
- [ ] Public keys read out:

```bash
kubectl exec -n mail deploy/dms -- \
  cat /tmp/docker-mailserver/opendkim/keys/example.org/dms.txt
```

- [ ] TXT records `dms._domainkey.<domain>` created for all four domains.
- [ ] TXT records are really TXT, not CNAME (checked with `dig`).
- [ ] Test mail is signed with `d=<From domain>` and `s=dms`.
- [ ] External DKIM verifier reports `dkim=pass`.

### 1.5 Functional Tests

- [ ] Without SMTP AUTH submission is rejected.
- [ ] With auth and an allowed domain, an arbitrary local part is accepted.
- [ ] With auth and a foreign domain, it is rejected.
- [ ] With an allowed envelope and a foreign `From:`, it is rejected.
- [ ] Port 465: implicit TLS works.
- [ ] Port 587: STARTTLS works; plaintext submission is refused.
- [ ] Queue ID appears in the log; `postqueue -p` works.
- [ ] A temporary 4xx error produces `deferred`, not an immediate bounce.
- [ ] A permanent 5xx error produces `bounced`.
- [ ] DSN lands at `bounces@example.org`.
- [ ] Thunderbird and FairEmail can send.
- [ ] At least one internal app (e.g. Gitea/Uptime Kuma) can send via 587.
- [ ] `250` is not interpreted as "arrived in the inbox".

### 1.6 Go-live

- [ ] DMARC initially `p=none`; watch reports.
- [ ] After 1-2 weeks without anomalies, move to `quarantine`, later `reject`.
- [ ] Check queue, logs and blocklists regularly.
- [ ] Backup job for config, state and DKIM keys set up.
- [ ] IP, PTR, DKIM selector and DNS state documented in the repo.

## 2. Server Migration Checklist

### 2.1 Preparation (T-7 to T-1)

- [ ] Inventory: domains, accounts, bounce address, DKIM selector, old/new IP, ports.
- [ ] Backup of config, state (including queue), mail, logs and secrets.
- [ ] Restore practiced on a test system.
- [ ] A record TTL reduced in time (currently already low).
- [ ] New server prepared: k3s, StorageClass, ArgoCD.
- [ ] New firewall: inbound 465/587 allowed, inbound 25 stays closed, outbound 25/53 allowed; if ACME HTTP-01 is used, also 80/443.
- [ ] TLS certificate for `mx.example.com` ready on the new server.
- [ ] New IP set at the hoster for PTR `mx.example.com`.
- [ ] SPF special case checked: `a:mx.example.com` follows the A record automatically; check old `ip4:` entries.
- [ ] Drain the old queue (`postqueue -f`, wait until empty).
- [ ] Stop the old server after draining so no duplicate sends occur.

### 2.2 Move Data

- [ ] Stop the old DMS (`replicas: 0`) after an empty queue.
- [ ] Copy PVC data: `config`, `state`, `mail`, `log`.
- [ ] Alternatively use a storage snapshot; do not copy from a running pod.
- [ ] Transfer the Secrets `dms-account` and `dms-tls` to the new cluster.
- [ ] Carry over the DKIM keys from `config/opendkim`; do not regenerate them if the DNS records are to remain.
- [ ] Start the new DMS; check config and DKIM paths.
- [ ] Test internal login and submission.

### 2.3 Cutover

- [ ] Set the PTR of the new IP to `mx.example.com`.
- [ ] Switch the A record `mx.example.com` to the new IP.
- [ ] Check FCrDNS: PTR -> A -> new IP.
- [ ] Check SPF: with `a:mx.example.com` no change is needed; replace old `ip4:` entries.
- [ ] Leave the MX records of the sender domains unchanged.
- [ ] Test the TLS connection on 465/587.
- [ ] Send a test mail; check DKIM/SPF/DMARC.
- [ ] Watch queue and logs.
- [ ] Bounce test to `bounces@example.org`.
- [ ] Old server stopped and PVC kept for at least 7 days for rollback.

### 2.4 Rollback

- [ ] Keep the old server and PVC for at least 7 days.
- [ ] Point the A record back to the old IP.
- [ ] The PTR of the old IP remains/is `mx.example.com`.
- [ ] Restore SPF if needed.
- [ ] Drain or deliberately migrate the queue on the new server before switching back.
- [ ] Inform clients if DNS caching causes problems.

### 2.5 Decommissioning

- [ ] After 7-14 days of stability: shut down the old server.
- [ ] Remove old DNS entries and old secrets.
- [ ] Rotate passwords and DKIM keys if the old server might have been compromised.
- [ ] Update documentation with the new IP, PTR and date.

## 3. Quick Commands

```bash
# DNS
dig +short A mx.example.com
dig +short -x 203.0.113.10
dig +short MX example.org
dig +short TXT example.org
dig +short TXT dms._domainkey.example.org
dig +short TXT _dmarc.example.org

# TLS
openssl s_client -connect mx.example.com:465 -servername mx.example.com -brief
openssl s_client -connect mx.example.com:587 -servername mx.example.com -starttls smtp -brief

# Queue / logs
kubectl exec -n mail deploy/dms -- postqueue -p
kubectl exec -n mail deploy/dms -- postcat -q <QUEUE_ID>
kubectl exec -n mail deploy/dms -- tail -n 100 /var/log/mail/mail.log
kubectl logs -n mail deploy/dms

# Apply a config change
kubectl rollout restart deployment/dms -n mail
```

### Test Outbound Port 25

**Result 2026-10-31:** successfully tested from the VPS; `nc -4 -vz -w 5 mx00.gmx.net 25` reported `succeeded` (212.227.15.150).

Important: **run this on the VPS / the k3s node**, not on your own Windows PC. The test measures the server's IP, not the home network's.

```bash
# Look up the GMX MX
dig +short MX gmx.net

# Test the TCP connection (IPv4)
nc -4 -vz -w 5 mx00.gmx.net 25

# Without netcat: read the banner
timeout 10 bash -c 'exec 3<>/dev/tcp/mx00.gmx.net/25; head -1 <&3'

# Optional: test STARTTLS of the target MX
timeout 10 openssl s_client -4 -connect mx00.gmx.net:25 -starttls smtp -brief
```

Expectation:

- `nc`: `succeeded` or `Connected`.
- Bash variant: `220 mx00.gmx.net ...`.
- `openssl`: TLS handshake successful.

If a timeout or `Connection refused` occurs, the VPS provider blocks outbound port 25 or the target MX filters the IP. Then:

1. Ask the VPS provider to unblock port 25.
2. Alternatively use a smarthost/relay (DMS: `RELAY_HOST`, `RELAY_PORT`, `RELAY_USER`, `RELAY_PASSWORD`).
3. Do not send a test mail to foreign recipients; the TCP banner is enough for the port test.

## 4. Current Known Pitfalls

- [ ] Wildcard CNAMEs hide `_dmarc` and `dms._domainkey` for `example.org`, `example.net`, `example.info`. Create and verify explicit TXT records.
- [ ] The SPF lookup limit is arithmetically at 10 for several domains; check with a validator.
- [ ] `mx.example.com` has a self-MX but currently no SMTP service on port 25.
- [ ] Port 25 stays closed for outbound-only; the PTR needs no open port.
- [ ] The TLS source (cert-manager, wildcard, manual) still needs to be decided.
- [ ] `local-path` is node-local; a server migration requires a data copy or snapshot.
- [ ] NetworkPolicy is not enforced with Flannel.
