#!/bin/sh
# One-time setup of encrypted off-server backups (restic -> Backblaze B2) on the VPS.
# Run it as root in an interactive SSH session: it asks for the storage keys and the backup
# password, and nothing you type is shown.
#
#   sudo sh setup-backup.sh <path to nexus-backup>   install, or change the keys
#   sudo sh setup-backup.sh --remove                  remove from this server (backups already in B2 stay)
#
# It installs restic (apt), installs /usr/local/sbin/nexus-backup, writes /etc/nexus-backup (root
# only), prepares the encrypted storage, and schedules a daily backup at 03:17 UTC (systemd timer).
# It does not run a backup and does not touch Docker, the app or its data.
set -eu
umask 077
ETC=/etc/nexus-backup
BIN=/usr/local/sbin/nexus-backup
UNIT_DIR=/etc/systemd/system

say() { printf '%s\n' "$*"; }
fail() { say "setup stopped: $*"; exit 1; }
matches() { printf '%s' "$1" | grep -Eqx -- "$2"; }
[ "$(id -u)" = 0 ] || fail "run it with sudo"
trap 'stty echo 2>/dev/null || true' EXIT INT TERM

if [ "${1:-}" = --remove ]; then
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now nexus-backup.timer >/dev/null 2>&1 || true
    fi
    rm -f "$UNIT_DIR/nexus-backup.service" "$UNIT_DIR/nexus-backup.timer" "$BIN"
    command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload >/dev/null 2>&1 || true
    rm -rf "$ETC" /var/lib/nexus-backup /var/cache/restic
    say "removed: daily backup schedule, $BIN, $ETC (keys and password), local backup cache"
    say "Backups already stored in Backblaze stay there; delete the bucket in Backblaze to remove them."
    exit 0
fi

src=${1:-}
[ -n "$src" ] && [ -f "$src" ] || fail "usage: sudo sh $0 <path to nexus-backup>"

ask() {  # prompt, hidden(0/1) -> prints the answer
    printf '%s' "$1" >&2
    if [ "$2" = 1 ] && [ -t 0 ]; then stty -echo; fi
    IFS= read -r answer || answer=""
    if [ "$2" = 1 ] && [ -t 0 ]; then stty echo; printf '\n' >&2; fi
    printf '%s' "$answer"
}

say "1/6 restic (backup tool)"
if ! command -v restic >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o DPkg::Lock::Timeout=300 restic > /dev/null \
        || fail "could not install restic with apt"
fi
say "    $(restic version | cut -d' ' -f1-2)"

say "2/6 backup program -> $BIN"
install -m 755 -o root -g root "$src" "$BIN"

say "3/6 storage details (paste each one, then press Enter)"
if [ -n "${NEXUS_BACKUP_TEST_REPOSITORY:-}" ]; then   # only used by the automated tests
    repo=$NEXUS_BACKUP_TEST_REPOSITORY
    bucket=${repo##*/}
    endpoint=${repo#s3:*://}
    endpoint=${endpoint%%/*}
    region=us-east-1
else
    bucket=$(ask "    Bucket name: " 0)
    matches "$bucket" '[A-Za-z0-9-]{6,50}' || fail "that does not look like a bucket name (6-50 letters, digits or dashes)"
    endpoint=$(ask "    Endpoint (from the bucket page, like s3.eu-central-003.backblazeb2.com): " 0)
    endpoint=${endpoint#https://}
    endpoint=${endpoint%/}
    matches "$endpoint" 's3\.[a-z0-9-]+\.backblazeb2\.com' || fail "the endpoint should look like s3.eu-central-003.backblazeb2.com"
    region=${endpoint#s3.}
    region=${region%.backblazeb2.com}
    repo="s3:https://$endpoint/$bucket"
fi
key_id=$(ask "    keyID (hidden): " 1)
matches "$key_id" '[0-9A-Za-z]{12,40}' || fail "that does not look like a Backblaze keyID"
app_key=$(ask "    applicationKey (hidden): " 1)
matches "$app_key" '[A-Za-z0-9+/=]{20,80}' || fail "that does not look like a Backblaze applicationKey"
pw1=$(ask "    Backup password from your password manager (hidden): " 1)
pw2=$(ask "    Same password again (hidden): " 1)
[ "$pw1" = "$pw2" ] || fail "the two passwords are different"
[ "${#pw1}" -ge 16 ] || fail "use a password of at least 16 characters"

say "4/6 settings -> $ETC (readable by root only)"
install -d -m 700 -o root -g root "$ETC"
printf '%s' "$pw1" > "$ETC/password.new"
{
    say "# Written by setup-backup.sh. Root only. Do not share."
    say "RESTIC_REPOSITORY='$repo'"
    say "RESTIC_PASSWORD_FILE='$ETC/password'"
    say "AWS_ACCESS_KEY_ID='$key_id'"
    say "AWS_SECRET_ACCESS_KEY='$app_key'"
    say "AWS_DEFAULT_REGION='$region'"
    say "NEXUS_BACKUP_BUCKET='$bucket'"
    say "NEXUS_BACKUP_ENDPOINT='$endpoint'"
} > "$ETC/restic.env.new"
chmod 600 "$ETC/password.new" "$ETC/restic.env.new"
mv -f "$ETC/password.new" "$ETC/password"
mv -f "$ETC/restic.env.new" "$ETC/restic.env"
pw1= pw2= app_key=

say "5/6 storage"
set -a
. "$ETC/restic.env"
set +a
export RESTIC_CACHE_DIR=/var/cache/restic
out=$(restic cat config 2>&1) && existing=1 || existing=0
if [ "$existing" = 1 ]; then
    say "    ok: storage already prepared, and the password opens it"
elif printf '%s' "$out" | grep -qi "wrong password"; then
    fail "this bucket already holds backups made with a different password"
else
    if out=$(restic init 2>&1); then
        say "    ok: encrypted storage prepared"
    else
        say "$out" | tail -n 3 | sed 's/^/    /'
        fail "could not prepare the storage: check the bucket name, endpoint, keyID and applicationKey"
    fi
fi

say "6/6 daily schedule (03:17 UTC)"
cat > "$UNIT_DIR/nexus-backup.service" <<'EOF'
[Unit]
Description=Nexus encrypted off-server backup (restic to Backblaze B2)
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/nexus-backup run
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
TimeoutStartSec=3h
EOF
cat > "$UNIT_DIR/nexus-backup.timer" <<'EOF'
[Unit]
Description=Daily Nexus encrypted off-server backup

[Timer]
OnCalendar=*-*-* 03:17:00 UTC
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF
chmod 644 "$UNIT_DIR/nexus-backup.service" "$UNIT_DIR/nexus-backup.timer"
if command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload 2>/dev/null \
        && systemctl enable --now nexus-backup.timer >/dev/null 2>&1; then
    say "    ok: next run $(systemctl show nexus-backup.timer -p NextElapseUSecRealtime --value 2>/dev/null)"
else
    say "    WARNING: could not start the daily schedule; backups only run when started from GitHub"
fi

say ""
say "Done. Nothing has been backed up yet: the first backup runs from GitHub (operation 'backup',"
say "argument 'run') or tonight at 03:17 UTC."
say "Keep the backup password in your password manager. Without it the backups cannot be opened."
