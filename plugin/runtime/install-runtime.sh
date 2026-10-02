#!/usr/bin/env bash
set -euo pipefail
runtime_source=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
grant_user=''
if [[ ${1:-} == --grant-user && $# == 2 ]]; then
  grant_user=$2
elif [[ $# != 0 ]]; then
  echo 'Usage: sudo bash install-runtime.sh [--grant-user LINUX_USER]' >&2
  exit 2
fi
if [[ $(id -u) != 0 ]]; then
  echo 'Run this installer as root (sudo bash install-runtime.sh).' >&2
  exit 1
fi
for runtime_command in python3 systemctl getent useradd groupadd install; do
  command -v "$runtime_command" >/dev/null || { echo "Missing prerequisite: $runtime_command" >&2; exit 1; }
done
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'
[[ -f "$runtime_source/minecraft_runtime.py" && -f "$runtime_source/ervisio-minecraft.service" ]] || { echo 'The runtime package is incomplete.' >&2; exit 1; }
if [[ -n "$grant_user" ]]; then
  getent passwd "$grant_user" >/dev/null || { echo "Unknown Linux user: $grant_user" >&2; exit 1; }
  command -v usermod >/dev/null
fi
getent group ervisio-minecraft >/dev/null || groupadd --system ervisio-minecraft
if ! getent passwd ervisio-minecraft >/dev/null; then
  runtime_nologin=$(command -v nologin || true)
  [[ -n "$runtime_nologin" ]] || runtime_nologin=/bin/false
  useradd --system --gid ervisio-minecraft --home-dir /srv/ervisio/minecraft --no-create-home --shell "$runtime_nologin" ervisio-minecraft
fi
runtime_uid=$(id -u ervisio-minecraft)
runtime_shell=$(getent passwd ervisio-minecraft | cut -d: -f7)
if [[ "$runtime_uid" == 0 || ( "$runtime_shell" != */nologin && "$runtime_shell" != /bin/false ) ]]; then
  echo 'Existing ervisio-minecraft account is not a dedicated non-login service user; installation stopped.' >&2
  exit 1
fi
for runtime_dir in /usr/lib/ervisio-minecraft /srv/ervisio/minecraft; do
  [[ ! -L "$runtime_dir" ]] || { echo "Refusing symlink: $runtime_dir" >&2; exit 1; }
done
install -d -o root -g root -m 0755 /usr/lib/ervisio-minecraft
install -d -o ervisio-minecraft -g ervisio-minecraft -m 0750 /srv/ervisio/minecraft
for runtime_module in "$runtime_source"/*.py; do
  [[ -f "$runtime_module" && ! -L "$runtime_module" ]] || { echo "Invalid runtime module: $runtime_module" >&2; exit 1; }
  install -o root -g root -m 0644 "$runtime_module" /usr/lib/ervisio-minecraft/
done
install -o root -g root -m 0644 "$runtime_source/ervisio-minecraft.service" /etc/systemd/system/ervisio-minecraft.service
if [[ -n "$grant_user" ]]; then
  usermod -a -G ervisio-minecraft "$grant_user"
  echo "$grant_user can manage all Minecraft servers after signing out and signing in again."
fi
systemctl daemon-reload
systemctl enable ervisio-minecraft.service
systemctl restart ervisio-minecraft.service
if ! command -v java >/dev/null; then
  echo 'Runtime installed. Install a compatible Java JRE/JDK before starting Minecraft servers.'
fi
systemctl --no-pager status ervisio-minecraft.service
