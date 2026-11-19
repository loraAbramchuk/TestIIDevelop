#!/usr/bin/env bash
# Ubuntu 24.04 installer. Pin --ref to a reviewed commit/tag for a real server.
set -euo pipefail
umask 022
prefix=/opt/video-platform
repository=https://github.com/loraAbramchuk/TestIIDevelop.git
ref=work
domain= email= recipient= ssh_target= ssh_path=
project=video-platform
http_port=8080
no_systemd=false
existing_docker=false
while (($#)); do
    case "$1" in
        --prefix) prefix=$2; shift 2;;
        --repository) repository=$2; shift 2;;
        --ref) ref=$2; shift 2;;
        --project) project=$2; shift 2;;
        --http-port) http_port=$2; shift 2;;
        --domain) domain=$2; shift 2;;
        --email) email=$2; shift 2;;
        --backup-recipient) recipient=$2; shift 2;;
        --backup-ssh-target) ssh_target=$2; shift 2;;
        --backup-ssh-path) ssh_path=$2; shift 2;;
        --no-systemd) no_systemd=true; shift;;
        --use-existing-docker) existing_docker=true; shift;;
        *) echo "Unknown installer option: $1" >&2; exit 1;;
    esac
done
[[ $(id -u) == 0 ]] || { echo 'Run the installer with sudo/root' >&2; exit 1; }
# Trusted OS metadata, not application .env.
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || { echo 'Ubuntu 24.04 LTS is required' >&2; exit 1; }
[[ $prefix =~ ^/[a-zA-Z0-9_./-]+$ && $prefix != / && $project =~ ^[a-z0-9][a-z0-9_-]{0,49}$ ]] || { echo 'Invalid installation path/project' >&2; exit 1; }
[[ $ref =~ ^[a-zA-Z0-9][a-zA-Z0-9_./-]*$ ]] || { echo 'Invalid source ref' >&2; exit 1; }
[[ $http_port =~ ^[0-9]+$ && $http_port -ge 1024 && $http_port -le 65535 ]] || { echo 'Local HTTP port must be 1024-65535' >&2; exit 1; }
[[ -z $domain || -n $email ]] || { echo '--email is required with --domain' >&2; exit 1; }
[[ $repository != -* && $repository != *\?* && $repository != *\#* ]] || { echo 'Invalid repository URL' >&2; exit 1; }
[[ ! $repository =~ ^https?://[^/]*@ ]] || { echo 'Use SSH credentials instead of tokens in repository URLs' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl git python3 age openssh-client certbot openssl
if ! docker compose version >/dev/null 2>&1; then
    # Ubuntu's maintained packages avoid an additional APT signing/repository dependency.
    apt-get install -y docker.io docker-compose-v2 docker-buildx
fi
python3 - "$(docker compose version --short)" <<'PY'
import re,sys
parts=tuple(map(int,re.search(r'(\d+)\.(\d+)\.(\d+)',sys.argv[1]).groups()))
assert parts >= (2,24,4), 'Docker Compose >= 2.24.4 is required'
PY
if ! $existing_docker; then
    systemctl enable --now docker
fi
docker info >/dev/null
export GIT_TERMINAL_PROMPT=0
if [[ ! -d $prefix/.git ]]; then
    [[ ! -e $prefix || -z $(ls -A "$prefix") ]] || { echo 'Installation directory must be empty' >&2; exit 1; }
    git clone --no-checkout "$repository" "$prefix"
else
    [[ $(git -C "$prefix" remote get-url origin) == "$repository" ]] || { echo 'Existing repository differs; configuration preserved' >&2; exit 1; }
    [[ -z $(git -C "$prefix" status --porcelain) ]] || { echo 'Existing checkout has local changes' >&2; exit 1; }
fi
git -C "$prefix" fetch origin "$ref"
target=$(git -C "$prefix" rev-parse FETCH_HEAD)
current=$(git -C "$prefix" rev-parse --verify HEAD 2>/dev/null || true)
if [[ -f $prefix/.env && -n $current && $current != "$target" ]]; then
    echo 'Existing release differs; use the backed-up update command' >&2; exit 1
fi
git -C "$prefix" checkout --detach "$target"
[[ -f $prefix/scripts/ops.py ]] || { echo 'Selected release does not include deployment tools' >&2; exit 1; }
ops=(python3 "$prefix/scripts/ops.py" --root "$prefix" --project "$project")
settings=()
[[ -z $domain ]] || settings+=(--domain "$domain" --email "$email")
[[ -z $recipient ]] || settings+=(--recipient "$recipient")
[[ -z $ssh_target ]] || settings+=(--ssh-target "$ssh_target")
[[ -z $ssh_path ]] || settings+=(--ssh-path "$ssh_path")
had_env=false
[[ ! -f $prefix/.env ]] || had_env=true
"${ops[@]}" init "${settings[@]}"
# Preserve an existing port, including custom operator configuration.
if ! $had_env; then
    printf '\nHTTP_PORT=%s\n' "$http_port" >> "$prefix/.env"
fi
configuration=$("${ops[@]}" configuration)
production=$(python3 -c 'import json,sys;print("yes" if json.load(sys.stdin)["production"] else "no")' <<< "$configuration")
backup_configured=$(python3 -c 'import json,sys;print("yes" if json.load(sys.stdin)["backup_configured"] else "no")' <<< "$configuration")
if [[ $production == yes ]]; then
    ops+=(--production)
    "${ops[@]}" tls
fi
"${ops[@]}" start
install -d -m 0755 /usr/local/bin
{
    echo '#!/usr/bin/env bash'
    echo 'set -euo pipefail'
    printf 'exec python3 %q --root %q --project %q ' "$prefix/scripts/ops.py" "$prefix" "$project"
    [[ $production == no ]] || printf '%s ' --production
    printf '"$@"\n'
} > /usr/local/bin/video-platform
chmod 0755 /usr/local/bin/video-platform
if ! $no_systemd; then
    cat > /etc/systemd/system/video-platform.service <<'UNIT'
[Unit]
Description=Video platform stack
After=network-online.target docker.service
Requires=docker.service
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/video-platform start --no-build
TimeoutStartSec=3600
[Install]
WantedBy=multi-user.target
UNIT
    if [[ $production == yes ]]; then
        cat > /etc/systemd/system/video-platform-renew.service <<'UNIT'
[Unit]
Description=Renew video platform TLS certificate
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/video-platform renew
TimeoutStartSec=3600
UNIT
        cat > /etc/systemd/system/video-platform-renew.timer <<'UNIT'
[Unit]
Description=Check video platform TLS twice daily
[Timer]
OnCalendar=*-*-* 03,15:00:00
RandomizedDelaySec=30m
Persistent=true
[Install]
WantedBy=timers.target
UNIT
    fi
    if [[ $backup_configured == yes ]]; then
        cat > /etc/systemd/system/video-platform-backup.service <<'UNIT'
[Unit]
Description=Encrypted off-site video platform backup
After=network-online.target docker.service
[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/local/bin/video-platform backup
TimeoutStartSec=86400
TimeoutStopSec=600
UNIT
        cat > /etc/systemd/system/video-platform-backup.timer <<'UNIT'
[Unit]
Description=Daily video platform backup
[Timer]
OnCalendar=*-*-* 02:00:00
RandomizedDelaySec=15m
Persistent=true
[Install]
WantedBy=timers.target
UNIT
    fi
    systemctl daemon-reload
    systemctl enable --now video-platform.service
    [[ $production == no ]] || systemctl enable --now video-platform-renew.timer
    if [[ $backup_configured == yes ]]; then
        systemctl enable --now video-platform-backup.timer
    else
        echo 'Off-site backup timer not enabled: configure recipient and SSH destination first.'
    fi
fi
/usr/local/bin/video-platform status
echo 'Installation completed. Create the first administrator with the documented CLI.'
