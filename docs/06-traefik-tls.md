# Traefik TLS for mx.example.com

Date: 2026-10-31  
Status: pattern for issuing a public certificate via Traefik, without a dedicated router or Service.

## Goal

Traefik issues a public TLS certificate for `mx.example.com`, which is later used
as the Kubernetes Secret `dms-tls` for Docker Mailserver. SMTP itself does not
run through Traefik; Traefik only provides the certificate.

## Traefik Configuration

In `traefik/dynamic.yml`:

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

Important:

- No `routers` entry for `mx.example.com`.
- No `services` entry for `mx.example.com`.
- `mx.example.com` is not attached to the k3s router as a rule.
- `defaultGeneratedCert` is the documented Traefik feature for an
  ACME certificate without a router/service (Traefik v3.6).

## DNS Prerequisites

```text
mx.example.com    A       203.0.113.10
mail.example.com  CNAME   mx.example.com
```

Every name in the certificate must be resolvable via A/AAAA. A SAN without a
record causes issuance of the **entire** certificate to fail.

Rule: only include SANs that actually point to Traefik via DNS.

## ACME

- Resolver: `leresolver`
- Challenge: HTTP-01 on port 80
- Storage: `/path/to/traefik/data/acme.json`
- Ports 80 and 443 are reachable on `203.0.113.10`.
- Traefik renews the certificate itself.

## Verification

Check externally:

```bash
openssl s_client -connect mx.example.com:443 \
  -servername mx.example.com 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
```

Expected shape:

```text
subject=CN = mx.example.com
issuer=<your ACME CA>
X509v3 Subject Alternative Name:
    DNS:mx.example.com, DNS:mail.example.com
```

## Certificate as a Kubernetes Secret

On the Traefik host:

```bash
python3 scripts/extract-traefik-cert.py \
  --acme-json /path/to/traefik/data/acme.json \
  --domain mx.example.com \
  --secret
```

The script:

- looks for `mx.example.com` in `domain.main` or `domain.sans`
- decodes cert and key from `acme.json`
- writes `./dms-tls/tls.crt` and `./dms-tls/tls.key`
- verifies cert/key and SAN
- optionally creates the Secret `mail/dms-tls`

Without `--secret`, only extract:

```bash
python3 scripts/extract-traefik-cert.py
```

## Renewal

Traefik renews the certificate in `acme.json`. The Kubernetes Secret is a
copy and is **not** updated automatically.

Options:

1. Extract again manually after the Traefik renewal.
2. A host timer/cron that synchronizes `acme.json` to `dms-tls`.
3. Later cert-manager with DNS-01, if renewal should run fully in-cluster.

For the MVP, option 1 or 2 is sufficient.

## Limits

`defaultGeneratedCert` creates **one** certificate (main + SANs) and makes it the
default certificate for unknown SNI. For multiple separate certificates with
their own keys, Traefik ACME without a router/service is not the right approach.
That would require an external ACME client, cert-manager or dummy routers.
For this project the one certificate is enough.

## Troubleshooting

| Log | Meaning |
|---|---|
| `Cannot retrieve the ACME challenge for example.com (token "about.php")` | bot scan on `/.well-known/acme-challenge`; harmless |
| `Cannot retrieve ... mx.example.com (token "test")` | manual test; harmless |
| `Error while creating certificate store ... falling back` | normal while no certificate has been issued yet |
| `no valid A records found for <san>` | SAN without A/AAAA; the entire certificate fails |

After a fix, do not retry again in quick succession; Let's Encrypt has
failed-validation limits.

## Do Not Use

- `k3s-serving` from `kube-system`: internal Kubernetes API certificate, not a
  public mail certificate.
- Self-signed for SMTP submission: mail clients will not trust it.
- `mx.example.com` as a rule on the k3s router: would route HTTP traffic to the
  Kubernetes API server.
