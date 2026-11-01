# Kubernetes Bootstrap

Date: 2026-10-31  
Goal: direct Kubernetes deployment without a Docker Compose PoC.

## 1. Prerequisites

- k3s cluster with `kubectl` access.
- StorageClass `local-path` (k3s default) or another RWO StorageClass.
- `openssl` on the machine where the secrets are created.
- Optional: ArgoCD in the `argocd` namespace.
- The DNS items from `03-dns.md` are prepared.

## 2. Check and Generate Configuration

```bash
python scripts/generate.py
python scripts/generate.py --check
```

Result:

- `manifests/10-configmap-env.yaml`
- `manifests/11-configmap-files.yaml`

After every change to `config/domains.yaml`, regenerate and commit the generated files. ConfigMaps are mounted via `subPath`; changes need a rollout.

## 3. Create Secrets

The secrets do not belong in Git. The script creates:

- `dms-account` with `postfix-accounts.cf` (SHA512-CRYPT)
- optionally `dms-tls`, if `TLS_CERT`/`TLS_KEY` are passed

```bash
bash scripts/bootstrap-secrets.sh
```

A self-signed certificate is **not** created by default. SMTP submission clients validate the certificate like HTTPS clients; self-signed is not a production state for that.

For a pure lab/smoke test it can be enabled deliberately:

```bash
bash scripts/bootstrap-secrets.sh --self-signed
# or
SELF_SIGNED=1 bash scripts/bootstrap-secrets.sh
```

This creates a certificate for `mx.example.com` and `mail.example.com` with a 30-day lifetime and creates `dms-tls`. Mail clients will not trust it.

### Extract TLS from Traefik

Traefik provides the certificate without a router/service via `defaultGeneratedCert`:

```yaml
tls:
  stores:
    default:
      defaultGeneratedCert:
        resolver: leresolver
        domain:
          main: mx.example.com
          sans:
            - mail.example.com
```

Details, DNS prerequisites and troubleshooting: [Traefik TLS](06-traefik-tls.md).

On the Docker host where Traefik runs:

```bash
python3 scripts/extract-traefik-cert.py
```

The script reads by default:

```text
/path/to/traefik/data/acme.json
```

The path is configurable via argument:

```bash
python3 scripts/extract-traefik-cert.py \
  --acme-json /other/path/acme.json \
  --domain mx.example.com \
  --out-dir ./dms-tls
```

It writes `./dms-tls/tls.crt` and `./dms-tls/tls.key`, verifies cert/key and SAN, and prints the available domains if `mx.example.com` is not found. Optionally directly as a Secret:

```bash
python3 scripts/extract-traefik-cert.py --secret
```

If no certificate for `mx.example.com` is present in `acme.json`, Traefik must issue it first. `k3s-serving` from `kube-system` is **not** suitable: that is the internal Kubernetes API certificate, not a public mail certificate.

Alternatively pass an existing certificate:

```bash
TLS_CERT=/path/tls.crt TLS_KEY=/path/tls.key bash scripts/bootstrap-secrets.sh
```

The script prompts for the SMTP password for `smtp@mail.example.com`. The password does not end up in Git or in the shell history.

## 4. Apply Manifests

```bash
kubectl apply -f manifests/
kubectl -n mail rollout status deployment/dms --timeout=5m
```

The order is not critical due to the file names and Kubernetes; namespace, ConfigMaps, PVC, Deployment and Service are created.

Check:

```bash
kubectl -n mail get pods
kubectl -n mail get svc dms
kubectl -n mail logs deploy/dms -c bootstrap
kubectl -n mail logs deploy/dms -c dms
```

The `EXTERNAL-IP` of the Service must match the IP of `mx.example.com` (`203.0.113.10`). With k3s ServiceLB this is normally the node IP.

The pod must become `Running` and `Ready`. The init container `bootstrap` copies the account file and creates missing DKIM keys.

## 5. DKIM Public Keys and DNS

Read the public key per domain:

```bash
for d in example.org example.net example.com example.info; do
  echo "=== $d ==="
  kubectl -n mail exec deploy/dms -- \
    cat "/tmp/docker-mailserver/opendkim/keys/$d/dms.txt"
done
```

Then create an **explicit TXT record** for each domain:

```text
dms._domainkey.<domain>  TXT  "v=DKIM1; k=rsa; p=<PUBLIC_KEY>"
```

Check:

```bash
dig +short TXT dms._domainkey.example.org
```

Important: The record must appear as TXT, not as CNAME. The existing wildcard CNAMEs must no longer hide the names.

DMARC:

- `example.com` already has `v=DMARC1;p=reject;`.
- For the other domains set `p=none` explicitly first, tighten later.

## 6. Test SMTP

From a machine with network access to 465/587:

```bash
# No auth -> must be rejected
swaks --server mail.example.com --port 587 --tls \
  --from test@example.org --to test@example.net

# Auth + allowed domain + arbitrary local part
swaks --server mail.example.com --port 587 --tls \
  --auth-user smtp@mail.example.com --auth-password '<PASSWORD>' \
  --from typo@example.org --to test@example.net

# Auth + foreign domain -> must be rejected
swaks --server mail.example.com --port 587 --tls \
  --auth-user smtp@mail.example.com --auth-password '<PASSWORD>' \
  --from hans@gmail.com --to test@example.net

# Allowed envelope, foreign From header -> must be rejected at DATA
swaks --server mail.example.com --port 587 --tls \
  --auth-user smtp@mail.example.com --auth-password '<PASSWORD>' \
  --from test@example.org \
  --header 'From: hans@gmail.com' \
  --to test@example.net
```

The tests assume a trusted certificate for `mail.example.com`. On certificate problems, check first with `openssl s_client`.

Thunderbird/FairEmail settings: [Mail clients](07-mail-clients.md).

## 7. ArgoCD

Adjust `argocd/application.yaml`:

```yaml
source:
  repoURL: https://gitea.example.com/<owner>/k8s-mail-infra.git
  targetRevision: main   # e.g. feat-001-init until the branch is merged
  path: manifests
```

Then:

```bash
kubectl apply -f argocd/application.yaml
```

The secrets must exist before the sync. ArgoCD does not manage them.

If the Application should live in the homelab repo (`example-homelab-cluster`), create an Application there with `source.path: manifests` and `source.repoURL` pointing to this repo. This file is then only a template.

## 8. Production TLS

A trusted certificate for `mail.example.com` is mandatory. `--self-signed` is intended only for lab/smoke tests.

1. Obtain a certificate: Traefik ACME, cert-manager, wildcard or manual.
2. Update the Secret:

```bash
TLS_CERT=/path/tls.crt TLS_KEY=/path/tls.key \
  bash scripts/bootstrap-secrets.sh
```

Or from Traefik:

```bash
python3 scripts/extract-traefik-cert.py --secret
```

3. Restart the pod:

```bash
kubectl -n mail rollout restart deployment/dms
```

With Traefik ACME, the Secret must be synchronized again after Traefik renewal. A host timer/cron or later cert-manager/DNS-01 handles this.

## 9. Changes and Rollback

ConfigMap change:

```bash
python scripts/generate.py
kubectl apply -f manifests/
kubectl -n mail rollout restart deployment/dms
```

DKIM key backup:

```bash
kubectl -n mail exec deploy/dms -- \
  tar czf - /tmp/docker-mailserver/opendkim > dkim-backup.tgz
```

Do not delete the PVC while the queue or the DKIM keys are needed. `kubectl delete -f manifests/` would also affect the PVC; for a controlled teardown, delete resources individually and keep the PVC.

## 10. Known Startup Problems

- Pod `Pending`: StorageClass `local-path` missing or PVC not bindable.
- Init container error: `kubectl -n mail logs deploy/dms -c bootstrap`; usually a missing Secret `dms-account` or `dms-tls`.
- DMS does not start: check the account file (`smtp@mail.example.com|{SHA512-CRYPT}$6$...`).
- No SMTP AUTH: Dovecot must run; `SMTP_ONLY` must not be set.
- No delivery: check outbound TCP 25 and the logs.
- DKIM red: the TXT record is still a CNAME or the public key does not match.
- No effect after a ConfigMap change: `subPath` mounts do not update; a rollout is needed.
