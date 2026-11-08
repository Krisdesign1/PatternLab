#!/bin/bash
# Stage a new installation. No package download, service start or enablement.
set -euo pipefail

fail() { printf '%s\n' "$1" >&2; exit 1; }
usage() { printf '%s\n' 'Usage: sudo bash /opt/patternlab/deployment/linux/install.sh --domain YOUR_PUBLIC_DNS_NAME'; }
if [[ "${1:-}" == --help ]]; then usage; exit 0; fi
[[ $# == 2 && "$1" == --domain ]] || { usage >&2; exit 1; }
[[ $EUID == 0 ]] || fail 'Run this staging installer as root on the Linux host.'
kit_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
[[ "$kit_dir" == /opt/patternlab/deployment/linux ]] || fail 'Stage the application at /opt/patternlab first.'
python3 "$kit_dir/preflight.py" --platform-only

for required in /opt/patternlab/app.py /opt/patternlab/scripts/automation_service.py /opt/patternlab/.venv/bin/python /usr/bin/caddy; do
    [[ -f "$required" ]] || fail 'A required application, Linux interpreter or Caddy binary is missing.'
done
getent passwd caddy >/dev/null || fail 'Install the Caddy package/service account first.'
[[ "$(id -u caddy)" != 0 ]] || fail 'The Caddy service account must be non-root.'
if systemctl is-active --quiet caddy.service || systemctl is-enabled --quiet caddy.service; then
    fail 'The stock Caddy service is active or enabled. Review it before using the separate PatternLab proxy.'
fi
# Avoid modifying an existing installation or its explicit cutover decision.
for target in /etc/patternlab/Caddyfile /etc/patternlab/web.env /etc/patternlab/worker-cutover-approved /etc/systemd/system/patternlab-web.service /etc/systemd/system/patternlab-worker.service /etc/systemd/system/patternlab-worker.timer /etc/systemd/system/patternlab-proxy.service; do
    [[ ! -e "$target" && ! -L "$target" ]] || fail 'A PatternLab deployment control already exists; review upgrades manually.'
done
for unit in patternlab-web.service patternlab-worker.service patternlab-worker.timer patternlab-proxy.service; do
    if systemctl is-active --quiet "$unit" 2>/dev/null || systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        fail 'A PatternLab unit is already active or enabled; review upgrades manually.'
    fi
done
for directory in /opt /opt/patternlab /opt/patternlab/deployment /opt/patternlab/deployment/linux /opt/patternlab/scripts /opt/patternlab/.venv /opt/patternlab/.venv/bin; do
    [[ -d "$directory" && ! -L "$directory" ]] || fail 'Application control directories must be real directories.'
done
# The data and supplied secrets are deliberately excluded: they belong to the
# service account. Everything executable stays root-owned and non-writable.
unsafe_path="$(find /opt/patternlab \( -path /opt/patternlab/data -o -path /opt/patternlab/.streamlit/secrets.toml \) -prune -o \( -type f -o -type d \) \( ! -user root -o -perm /022 \) -print -quit)"
[[ -z "$unsafe_path" ]] || fail 'Application source and the Linux venv must be root-owned and not group/world writable.'
for directory in /etc/patternlab /opt/patternlab/data /opt/patternlab/.streamlit /var/lib/patternlab; do
    [[ ! -L "$directory" ]] || fail 'Deployment directories must not be symbolic links.'
done

proxy_stage="$(mktemp /tmp/patternlab-caddy.XXXXXX)"
trap 'rm -f -- "$proxy_stage"' EXIT
python3 "$kit_dir/preflight.py" --render-caddy "$2" > "$proxy_stage"
# Adapt parses the file locally; it does not request a TLS certificate.
/usr/bin/caddy adapt --config "$proxy_stage" --adapter caddyfile >/dev/null

if ! id patternlab >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/patternlab --no-create-home --shell /usr/sbin/nologin patternlab
fi
[[ "$(id -u patternlab)" != 0 && "$(id -g patternlab)" != 0 && "$(id -gn patternlab)" == patternlab ]] || fail 'The patternlab account must use its own non-root group.'
[[ "$(getent passwd patternlab | cut -d: -f6)" == /var/lib/patternlab ]] || fail 'The patternlab home must be /var/lib/patternlab.'
[[ "$(getent passwd patternlab | cut -d: -f7)" == /usr/sbin/nologin ]] || fail 'The patternlab account must have the nologin shell.'

install -d -m 0755 -o root -g root /etc/patternlab
install -d -m 0750 -o root -g patternlab /opt/patternlab/.streamlit
install -d -m 0700 -o patternlab -g patternlab /var/lib/patternlab
if [[ ! -e /opt/patternlab/data ]]; then
    install -d -m 0700 -o patternlab -g patternlab /opt/patternlab/data
fi
install -m 0644 -o root -g root "$proxy_stage" /etc/patternlab/Caddyfile
install -m 0644 -o root -g root /dev/null /etc/patternlab/web.env
python3 "$kit_dir/preflight.py" --render-web-env "$2" > /etc/patternlab/web.env
for unit in patternlab-web.service patternlab-worker.service patternlab-worker.timer patternlab-proxy.service; do
    install -m 0644 -o root -g root "$kit_dir/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
printf '%s\n' 'Deployment controls staged. Every PatternLab service remains stopped and disabled.'
printf '%s\n' 'Supply Linux secrets and a consistent local-data snapshot, then run preflight as patternlab.'
printf '%s\n' 'The Mac worker remains authoritative until a separate deliberate cutover.'
