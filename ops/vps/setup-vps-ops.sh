#!/bin/sh
# One-time setup of the GitHub "VPS operations" pipeline on this server (Ubuntu).
#
#   sudo sh setup-vps-ops.sh <path to nexus-ops-runner>                  install, or replace the main key
#   sudo sh setup-vps-ops.sh --add-check-key <path to nexus-ops-runner>  install that runner and add (or
#                                                                        replace) the read-only check key
#   sudo sh setup-vps-ops.sh --remove                                    undo everything except the logs
#
# The main key (GitHub environment "vps", the owner approves each run) can start any operation.
# The check key (GitHub environment "vps-check", no per-run approval) makes the runner work in check
# mode: it accepts only the read-only checks built into the runner. Each install keeps the other key.
#
# What it sets up:
#   - user nexus-ops: no password, SSH key only, and that key can do exactly one thing:
#     start /usr/local/sbin/nexus-ops-runner (forced command, no terminal, no forwarding)
#   - /etc/sudoers.d/nexus-ops: nexus-ops may start the runner as root, nothing else
#     (checked with visudo before it is installed)
#   - a new SSH key pair; the private half is printed once for GitHub and then deleted here
# It does not touch Docker, the app, sshd_config, the firewall or any other user.
set -eu
umask 022
U=nexus-ops
HOME_DIR=/var/lib/nexus-ops
RUNNER=/usr/local/sbin/nexus-ops-runner
SUDOERS=/etc/sudoers.d/nexus-ops

say() { printf '%s\n' "$*"; }
fail() { say "setup stopped: $*"; exit 1; }
[ "$(id -u)" = 0 ] || fail "run it with sudo"

if [ "${1:-}" = --remove ]; then
    rm -f "$SUDOERS" "$RUNNER" "$HOME_DIR/.ssh/authorized_keys"
    if id "$U" >/dev/null 2>&1; then userdel "$U"; fi
    say "removed: user $U, its key, $SUDOERS and $RUNNER (logs kept in /var/log/nexus-ops)"
    exit 0
fi

kind=main
if [ "${1:-}" = --add-check-key ]; then kind=check; shift; fi
src=${1:-}
[ -n "$src" ] && [ -f "$src" ] || fail "usage: sudo sh $0 [--add-check-key] <path to nexus-ops-runner>"
if [ "$kind" = check ]; then
    grep -q 'nexus-ops-runner \\"' "$HOME_DIR/.ssh/authorized_keys" 2>/dev/null \
        || fail "the main key is not set up yet; run: sudo sh $0 <path to nexus-ops-runner>"
fi
for t in sshd ssh-keygen sudo visudo flock setsid timeout sha256sum su; do
    command -v "$t" >/dev/null 2>&1 || fail "$t is not installed"
done

say "1/6 user $U"
if ! id "$U" >/dev/null 2>&1; then
    useradd --system --home-dir "$HOME_DIR" --no-create-home --shell /bin/sh \
        --comment "GitHub VPS operations (forced command only)" "$U"
fi
usermod -p '*' "$U"   # no password can ever match: SSH key only
install -d -m 755 -o root -g root "$HOME_DIR" "$HOME_DIR/.ssh" /etc/nexus-ops
install -d -m 700 -o root -g root "$HOME_DIR/state" /var/log/nexus-ops

say "2/6 runner -> $RUNNER"
install -m 755 -o root -g root "$src" "$RUNNER"

say "3/6 sudo rule (checked with visudo before installing)"
tmp=$(mktemp)
printf '%s\n' "# GitHub VPS operations: $U may start only the runner, as root." \
    "$U ALL=(root) NOPASSWD: $RUNNER" > "$tmp"
if ! visudo -cqf "$tmp"; then rm -f "$tmp"; fail "the sudo rule did not pass visudo; nothing installed"; fi
install -m 440 -o root -g root "$tmp" "$SUDOERS"
rm -f "$tmp"
if ! visudo -cq; then rm -f "$SUDOERS"; fail "sudo configuration check failed; the new rule was removed again"; fi

if [ "$kind" = check ]; then flag="--check "; label=check; else flag=""; label=main; fi
say "4/6 new $label SSH key (replaces any earlier $label key; the other key is kept)"
kdir=$(mktemp -d)
trap 'rm -rf "$kdir"' EXIT
ssh-keygen -q -t ed25519 -N '' -C "github-vps-ops-$label $(hostname) $(date -u +%Y-%m-%d)" -f "$kdir/key"
keep=""
if [ -f "$HOME_DIR/.ssh/authorized_keys" ]; then
    if [ "$kind" = check ]; then
        keep=$(grep '^restrict,command="sudo -n [^ ]*nexus-ops-runner \\"' "$HOME_DIR/.ssh/authorized_keys" || true)
    else
        keep=$(grep '^restrict,command="sudo -n [^ ]*nexus-ops-runner --check ' "$HOME_DIR/.ssh/authorized_keys" || true)
    fi
fi
{
    say "# GitHub VPS operations keys. They can only start $RUNNER. Managed by setup-vps-ops.sh."
    [ -n "$keep" ] && printf '%s\n' "$keep"
    printf 'restrict,command="sudo -n %s %s\\"$SSH_ORIGINAL_COMMAND\\"" %s\n' "$RUNNER" "$flag" "$(cat "$kdir/key.pub")"
} > "$HOME_DIR/.ssh/authorized_keys.new"
chmod 644 "$HOME_DIR/.ssh/authorized_keys.new"
mv -f "$HOME_DIR/.ssh/authorized_keys.new" "$HOME_DIR/.ssh/authorized_keys"

say "5/6 self-test (same path sshd uses: $U -> sudo -> runner)"
test_script="$kdir/selftest.sh"
printf 'echo nexus-ops-selftest-ok\n' > "$test_script"
sum=$(sha256sum "$test_script" | cut -d' ' -f1)
if [ "$kind" = check ]; then
    out=$(su -s /bin/sh "$U" -c "sudo -n $RUNNER --check 'run hello - $sum 0000000000000000000000000000000000000000 $(date +%s)1'" \
        < "$test_script" 2>&1) || true
    printf '%s\n' "$out" | grep -q '^nexus-ops-selftest-ok$' || fail "self-test failed:
$out"
    out=$(su -s /bin/sh "$U" -c "sudo -n $RUNNER --check 'run p1b-stop-staging stop $sum 0000000000000000000000000000000000000000 $(date +%s)2'" \
        < "$test_script" 2>&1) || true
    printf '%s\n' "$out" | grep -q "only start read-only checks" || fail "self-test failed: check mode accepted a change:
$out"
    say "    ok: check mode runs a read-only check and refuses a change"
else
    out=$(su -s /bin/sh "$U" -c "sudo -n $RUNNER 'run selftest - $sum 0000000000000000000000000000000000000000 $(date +%s)'" \
        < "$test_script" 2>&1) || true
    printf '%s\n' "$out" | grep -q '^nexus-ops-selftest-ok$' || fail "self-test failed:
$out"
    say "    ok"
fi

say "6/6 sshd settings that could block GitHub"
cfg=$(sshd -T 2>/dev/null || true)
port=$(printf '%s\n' "$cfg" | awk '$1 == "port" {print $2; exit}')
printf '%s\n' "$cfg" | grep -Eiq '^pubkeyauthentication no' && say "    WARNING: sshd has PubkeyAuthentication no"
limits=$(printf '%s\n' "$cfg" | grep -Ei '^(allowusers|allowgroups|denyusers|denygroups) ' || true)
if [ -n "$limits" ]; then
    say "    WARNING: sshd limits who may log in; make sure $U is allowed:"
    printf '%s\n' "$limits" | sed 's/^/      /'
else
    say "    ok: no AllowUsers/AllowGroups limits"
fi
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    say "    firewall (ufw) rules for SSH:"
    ufw status 2>/dev/null | grep -Ei "(^${port:-22}[ /]|openssh)" | sed 's/^/      /' || true
fi

host_key=$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)
say ""
if [ "$kind" = check ]; then envname=vps-check; else envname=vps; fi
say "================ Paste these into GitHub: Settings > Environments > $envname ================"
say ""
say "Variable  VPS_HOST_KEY  ="
say "$host_key"
say ""
say "Variable  VPS_SSH_PORT  =  ${port:-22}"
say ""
say "Secret    VPS_SSH_KEY   =  everything between the two lines of dashes below, including"
say "                           the BEGIN and END lines. Paste it ONLY into GitHub, never into a chat."
say "-----------------------------------------------------------------------------------------"
cat "$kdir/key"
say "-----------------------------------------------------------------------------------------"
say "The private key is now deleted from this server. To make a new one, run this script again."
