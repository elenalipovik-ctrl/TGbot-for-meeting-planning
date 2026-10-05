#!/usr/bin/env bash
# Install Docker Engine and Compose on the approved Ubuntu VPS.
# Run as root after checking that no conflicting Docker packages are installed.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo 'Run this installer with sudo.' >&2
  exit 1
fi

. /etc/os-release
if [[ ${ID} != ubuntu || ${VERSION_CODENAME} != resolute ]]; then
  echo 'Expected Ubuntu 26.04 (resolute); inspect the VPS before installing.' >&2
  exit 1
fi

install -m 0755 -d /etc/apt/keyrings
curl -4 -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get -o Acquire::ForceIPv4=true update
DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl is-active --quiet docker
docker compose version
