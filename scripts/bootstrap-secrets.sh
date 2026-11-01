#!/usr/bin/env bash
set -euo pipefail

# Creates the two Kubernetes Secrets that do not belong in Git:
#   dms-account : postfix-accounts.cf with a SHA512-CRYPT hash
#   dms-tls     : TLS certificate and key
#
# Usage:
#   bash scripts/bootstrap-secrets.sh
#   SMTP_PASSWORD='...' bash scripts/bootstrap-secrets.sh
#   TLS_CERT=/path/tls.crt TLS_KEY=/path/tls.key bash scripts/bootstrap-secrets.sh
#   bash scripts/bootstrap-secrets.sh --self-signed
#   SELF_SIGNED=1 bash scripts/bootstrap-secrets.sh
#
# TLS priority:
#   1. TLS_CERT + TLS_KEY
#   2. existing dms-tls Secret (left unchanged)
#   3. --self-signed / SELF_SIGNED=1: self-signed test certificate
#   4. otherwise abort with a hint to the Traefik extraction
#
# Self-signed is intended ONLY for lab/smoke tests without real mail clients.
# For SMTP submission the certificate must be trusted by mail clients.

NAMESPACE="${NAMESPACE:-mail}"
ACCOUNT="${ACCOUNT:-smtp@mail.example.com}"
SMTP_PASSWORD="${SMTP_PASSWORD:-}"
TLS_CERT="${TLS_CERT:-}"
TLS_KEY="${TLS_KEY:-}"
SELF_SIGNED="${SELF_SIGNED:-0}"
SELF_SIGNED_DAYS="${SELF_SIGNED_DAYS:-30}"
HOSTNAME_VALUE="${HOSTNAME_VALUE:-mx.example.com}"
MAIL_HOSTNAME_VALUE="${MAIL_HOSTNAME_VALUE:-mail.example.com}"

usage() {
  cat <<'EOF'
Usage: bootstrap-secrets.sh [--self-signed]

Creates dms-account and, if possible, dms-tls in the Kubernetes namespace.

TLS sources, in order:
  1. TLS_CERT=/path/tls.crt TLS_KEY=/path/tls.key
  2. existing dms-tls Secret (left unchanged)
  3. --self-signed or SELF_SIGNED=1 (lab/smoke test only)

Self-signed certificates are not suitable for production SMTP submission.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --self-signed) SELF_SIGNED=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

case "$SELF_SIGNED" in
  0|1) ;;
  *) echo "SELF_SIGNED must be 0 or 1." >&2; exit 1 ;;
esac

case "$SELF_SIGNED_DAYS" in
  ''|*[!0-9]*) echo "SELF_SIGNED_DAYS must be a number." >&2; exit 1 ;;
esac

command -v kubectl >/dev/null 2>&1 || { echo "kubectl is missing" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "openssl is missing" >&2; exit 1; }

CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
if [ -z "$CONTEXT" ]; then
  echo "No kubectl context set." >&2
  exit 1
fi
echo "kubectl context: $CONTEXT"

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

if [ -z "$SMTP_PASSWORD" ]; then
  read -rsp "SMTP password for ${ACCOUNT}: " SMTP_PASSWORD
  echo
fi
[ -n "$SMTP_PASSWORD" ] || { echo "An empty password is not allowed." >&2; exit 1; }

umask 077
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

HASH="{SHA512-CRYPT}$(openssl passwd -6 "$SMTP_PASSWORD")"
printf '%s|%s\n' "$ACCOUNT" "$HASH" > "$TMP_DIR/postfix-accounts.cf"

kubectl -n "$NAMESPACE" create secret generic dms-account \
  --from-file="$TMP_DIR/postfix-accounts.cf" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

if [ -n "$TLS_CERT" ] || [ -n "$TLS_KEY" ]; then
  if [ -z "$TLS_CERT" ] || [ -z "$TLS_KEY" ]; then
    echo "ERROR: TLS_CERT and TLS_KEY must be provided together." >&2
    exit 1
  fi
  kubectl -n "$NAMESPACE" create secret tls dms-tls \
    --cert="$TLS_CERT" --key="$TLS_KEY" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "dms-tls created from the provided certificate."
elif kubectl -n "$NAMESPACE" get secret dms-tls >/dev/null 2>&1; then
  echo "dms-tls already exists; left unchanged."
  if [ "$SELF_SIGNED" -eq 1 ]; then
    echo "Note: --self-signed was ignored. Delete dms-tls if you want to replace it." >&2
  fi
elif [ "$SELF_SIGNED" -eq 1 ]; then
  echo "WARNING: Creating a self-signed certificate for ${HOSTNAME_VALUE} and ${MAIL_HOSTNAME_VALUE}."
  echo "WARNING: Lab/smoke tests only. Mail clients will not trust it." >&2

  if ! openssl req -x509 -newkey rsa:2048 -nodes -days "$SELF_SIGNED_DAYS" \
      -subj "/CN=${HOSTNAME_VALUE}" \
      -addext "subjectAltName=DNS:${HOSTNAME_VALUE},DNS:${MAIL_HOSTNAME_VALUE}" \
      -keyout "$TMP_DIR/tls.key" \
      -out "$TMP_DIR/tls.crt" >/dev/null 2>&1; then
    cat > "$TMP_DIR/openssl.cnf" <<EOF
[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_req
prompt = no
[req_distinguished_name]
CN = ${HOSTNAME_VALUE}
[v3_req]
subjectAltName = DNS:${HOSTNAME_VALUE}, DNS:${MAIL_HOSTNAME_VALUE}
EOF
    openssl req -x509 -newkey rsa:2048 -nodes -days "$SELF_SIGNED_DAYS" \
      -config "$TMP_DIR/openssl.cnf" \
      -keyout "$TMP_DIR/tls.key" \
      -out "$TMP_DIR/tls.crt" >/dev/null 2>&1
  fi

  kubectl -n "$NAMESPACE" create secret tls dms-tls \
    --cert="$TMP_DIR/tls.crt" --key="$TMP_DIR/tls.key" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "dms-tls created with a self-signed test certificate."
else
  cat >&2 <<EOF
ERROR: dms-tls is missing.

A self-signed certificate is not suitable for SMTP submission.
Options:
  1. On the Traefik host:
       python3 scripts/extract-traefik-cert.py --secret
  2. Provide an existing certificate:
       TLS_CERT=/path/tls.crt TLS_KEY=/path/tls.key bash scripts/bootstrap-secrets.sh
  3. Lab/smoke tests only:
       bash scripts/bootstrap-secrets.sh --self-signed

k3s-serving is not suitable for this: it is the internal
Kubernetes API certificate, not a public mail certificate.
EOF
  exit 1
fi

echo "Secrets created/updated in namespace '${NAMESPACE}'."
echo "Next step: sync the ArgoCD Application or run 'kubectl apply -f manifests/'."
