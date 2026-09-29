#!/usr/bin/env bash
# Create the internal certificate authority and the MinIO server certificate.
#
# OpenSSL 3 rejects a CA certificate that carries neither basicConstraints nor
# keyUsage, and the API image runs Python 3.13, so the API refused the MinIO
# server certificate on the first prepared host and /health/ready answered 503.
# Both extensions are therefore mandatory here, not decoration.
#
# The material is created once. A partial set is reported instead of being
# silently replaced, because a half-overwritten authority would invalidate the
# running MinIO container. Remove the material deliberately to force a new set.
set -Eeuo pipefail

DEPLOY_PATH="${DEPLOY_PATH:-/opt/ota-service}"
CERT_ROOT="${OTA_MINIO_CERT_ROOT:-${DEPLOY_PATH}/certs}"
CA_DAYS="${OTA_MINIO_CA_DAYS:-3650}"
LEAF_DAYS="${OTA_MINIO_LEAF_DAYS:-825}"
CA_SUBJECT="${OTA_MINIO_CA_SUBJECT:-/CN=OTA-Service internal MinIO CA}"
LEAF_SUBJECT="${OTA_MINIO_LEAF_SUBJECT:-/CN=minio}"
LEAF_NAMES="${OTA_MINIO_LEAF_NAMES:-DNS:host.docker.internal,DNS:minio,IP:127.0.0.1}"

AUTHORITY_DIR="${CERT_ROOT}/authority"
MINIO_DIR="${CERT_ROOT}/minio"

CA_KEY="${AUTHORITY_DIR}/ota-minio-ca.key"
CA_CERT="${AUTHORITY_DIR}/ota-minio-ca.crt"
LEAF_KEY="${MINIO_DIR}/private.key"
LEAF_CERT="${MINIO_DIR}/public.crt"
LEAF_CONFIG="${AUTHORITY_DIR}/minio-cert.cnf"

present=0
for candidate in "${CA_KEY}" "${CA_CERT}" "${LEAF_KEY}" "${LEAF_CERT}"; do
  if [[ -f "${candidate}" ]]; then
    present=$((present + 1))
  fi
done

if ((present == 4)); then
  echo "MinIO TLS material already present in ${CERT_ROOT}; leaving it unchanged."
  exit 0
fi

if ((present > 0)); then
  echo "Partial MinIO TLS certificate material found in ${CERT_ROOT}; refusing to overwrite it." >&2
  echo "Remove ${AUTHORITY_DIR} and ${MINIO_DIR} deliberately, then rerun." >&2
  exit 1
fi

umask 077
install -d -m 0700 "${AUTHORITY_DIR}"
install -d -m 0750 "${MINIO_DIR}"

openssl req -x509 -newkey rsa:3072 -nodes -days "${CA_DAYS}" -sha256 \
  -keyout "${CA_KEY}" \
  -out "${CA_CERT}" \
  -subj "${CA_SUBJECT}" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash"

{
  cat <<'EOF'
[req]
distinguished_name = distinguished_name
req_extensions = req_extensions
prompt = no

[distinguished_name]
CN = minio

[req_extensions]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alternate_names

[alternate_names]
EOF
  dns_i=1
  ip_i=1
  IFS=',' read -r -a names <<<"${LEAF_NAMES}"
  for entry in "${names[@]}"; do
    entry="${entry#"${entry%%[![:space:]]*}"}"
    entry="${entry%"${entry##*[![:space:]]}"}"
    case "${entry}" in
      DNS:*)
        printf 'DNS.%s = %s\n' "${dns_i}" "${entry#DNS:}"
        dns_i=$((dns_i + 1))
        ;;
      IP:*)
        printf 'IP.%s = %s\n' "${ip_i}" "${entry#IP:}"
        ip_i=$((ip_i + 1))
        ;;
      *)
        echo "Unsupported LEAF_NAMES entry: ${entry} (use DNS:name or IP:addr)" >&2
        exit 1
        ;;
    esac
  done
} >"${LEAF_CONFIG}"

chmod 0600 "${LEAF_CONFIG}"
openssl req -new -newkey rsa:2048 -nodes \
  -keyout "${LEAF_KEY}" \
  -out "${AUTHORITY_DIR}/minio.csr" \
  -config "${LEAF_CONFIG}"
openssl x509 -req -in "${AUTHORITY_DIR}/minio.csr" \
  -CA "${CA_CERT}" \
  -CAkey "${CA_KEY}" \
  -CAcreateserial \
  -out "${LEAF_CERT}" \
  -days "${LEAF_DAYS}" \
  -sha256 \
  -extfile "${LEAF_CONFIG}" \
  -extensions req_extensions
rm -f "${AUTHORITY_DIR}/minio.csr" "${AUTHORITY_DIR}/minio-cert.cnf" \
  "${AUTHORITY_DIR}/ota-minio-ca.srl"

# MinIO serves public.crt; keep the CA readable for the API container mount.
chmod 0640 "${LEAF_CERT}" "${CA_CERT}"
chmod 0600 "${LEAF_KEY}" "${CA_KEY}"

echo "Created a new MinIO certificate authority and server certificate in ${CERT_ROOT}."
echo "SANs: ${LEAF_NAMES}"
echo "Restart the MinIO container so it serves the new certificate."
