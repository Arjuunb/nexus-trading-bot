#!/bin/sh
# Test of ops/vps/operations/prod-si-images.sh. DISPOSABLE DOCKER HOSTS ONLY.
#   NEXUS_PROD_SI_TEST=disposable sh ops/vps/test/prod_si_images_test.sh   (from the repository root)
set -u
[ "${NEXUS_PROD_SI_TEST:-}" = disposable ] || { echo "refused: set NEXUS_PROD_SI_TEST=disposable (never on the VPS)"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 3 | tr '\n' '|'))"; fi; }
OP=$(pwd)/ops/vps/operations/prod-si-images.sh
REAL_FLAGS=$(pwd)/ops/vps/test/fixtures/feature_flags.py
T=$(mktemp -d)
COMMIT=1111111111111111111111111111111111111111
SOURCE=2222222222222222222222222222222222222222222222222222222222222222
export PROD_SI_TEST_COMMIT=$COMMIT PROD_SI_TEST_SOURCE=$SOURCE

# make_packet <name> <flags.py> <user> <webui js content> <vuln severity> [extra]
make_packet() {
    d=$T/$1; rm -rf "$d"; mkdir -p "$d/ctx/app/automation-hub/services" "$d/ctx/app/automation-hub/webui" "$d/packet"
    cp "$2" "$d/ctx/app/automation-hub/services/feature_flags.py"
    echo '<html></html>' > "$d/ctx/app/automation-hub/webui/index.html"
    printf '%s' "$4" > "$d/ctx/app/automation-hub/webui/app.js"
    printf 'FROM python:3.12-alpine\nLABEL org.opencontainers.image.revision=%s nexus.source.sha256=%s\nCOPY app /app\nUSER %s\n' "$COMMIT" "$SOURCE" "$3" > "$d/ctx/Dockerfile"
    docker build -q -t "nexus-ci-api:$COMMIT" "$d/ctx" >/dev/null
    id=$(docker image inspect "nexus-ci-api:$COMMIT" -f '{{.Id}}')
    docker save "nexus-ci-api:$COMMIT" -o "$d/packet/api.tar" && gzip -n "$d/packet/api.tar"
    docker rmi -f "nexus-ci-api:$COMMIT" >/dev/null
    printf '{"commit":"%s","source_sha256":"%s","images":{"api":{"config_id":"%s"}}}' "$COMMIT" "$SOURCE" "$id" > "$d/packet/build-proof.json"
    printf '{"head":"%s","source_sha256":"%s","files":[]}' "$COMMIT" "$SOURCE" > "$d/packet/source-manifest.json"
    printf '{"Results":[{"Vulnerabilities":[{"Severity":"%s"}]}]}' "$5" > "$d/packet/api-security.json"
    echo '{"Results":[]}' > "$d/packet/api-all-secrets.json"
    python3 - "$d/packet" "$COMMIT" "$SOURCE" <<'PY'
import hashlib, json, os, sys
root, commit, source = sys.argv[1:]
rows = [{"path": f, "bytes": os.path.getsize(os.path.join(root, f)), "sha256": hashlib.sha256(open(os.path.join(root, f), "rb").read()).hexdigest()}
        for f in sorted(os.listdir(root))]
json.dump({"commit": commit, "source_sha256": source, "files": rows}, open(os.path.join(root, "packet-integrity.json"), "w"))
PY
    [ "${6:-}" = extra ] && echo surprise > "$d/packet/extra.txt"
    [ "${6:-}" = tamper ] && echo '{"Results":[]}' > "$d/packet/api-security.json"
    (cd "$d/packet" && python3 -c "import zipfile,os; z=zipfile.ZipFile('../pkg.zip','w'); [z.write(f) for f in sorted(os.listdir('.'))]; z.close()")
    PROD_SI_TEST_ZIP=$d/pkg.zip; PROD_SI_TEST_ZIP_SHA256=$(sha256sum "$d/pkg.zip" | cut -d' ' -f1)
    PROD_SI_TEST_WORK=$d/work; export PROD_SI_TEST_ZIP PROD_SI_TEST_ZIP_SHA256 PROD_SI_TEST_WORK
}

docker rmi -f "nexus-ci-api:$COMMIT" >/dev/null 2>&1
ON=$T/flags_on.py; sed "s/'false').strip/'true').strip/" "$REAL_FLAGS" > "$ON"

SI='console.log("#/strategy-intelligence")'
make_packet good "$REAL_FLAGS" 100:101 "$SI" LOW
out=$(PROD_SI_TEST_ZIP_SHA256=$(printf 'x' | sha256sum | cut -d' ' -f1) sh "$OP" load); has "refuses a package with the wrong SHA-256" "$out" "BLOCKED: package SHA-256"
out=$(PROD_SI_TEST_ZIP=$T/nothing.zip sh "$OP" load); has "refuses a missing package" "$out" "BLOCKED: package not found"
out=$(sh "$OP" load); has "loads a good package" "$out" "READY: the Strategy Intelligence image is loaded and verified"
has "reports GitHub digest match" "$out" "has GitHub's recorded SHA-256"
has "reports sealed list" "$out" "sealed file list verified: 5 files"
has "reports scans" "$out" "0 HIGH/CRITICAL vulnerabilities, 0 secrets"
has "reports PA default off" "$out" "Price Action Lab default OFF"
has "reports the production user and SI page" "$out" "runs as 100:101 like production's data files"
id=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['image_id'])" "$PROD_SI_TEST_WORK/api-image.json")
[ "$(docker image inspect "$id" -f '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$COMMIT" ] && ok "recorded image ID is the loaded image" || bad "recorded image ID"
[ "$(docker ps -aq --filter ancestor="$id" | wc -l)" = 0 ] && ok "no container left from the probe" || bad "probe container left"
out=$(sh "$OP" load); has "second load is fine (idempotent)" "$out" "READY"

make_packet tamper "$REAL_FLAGS" 100:101 "$SI" LOW tamper
out=$(sh "$OP" load); has "refuses a file changed after sealing" "$out" "BLOCKED: sealed file changed: api-security.json"
make_packet extra "$REAL_FLAGS" 100:101 "$SI" LOW extra
out=$(sh "$OP" load); has "refuses an unsealed extra file" "$out" "BLOCKED: package has files that are not in its seal"
make_packet vuln "$REAL_FLAGS" 100:101 "$SI" CRITICAL
out=$(sh "$OP" load); has "refuses HIGH/CRITICAL findings" "$out" "BLOCKED: security reports list 1"
make_packet paon "$ON" 100:101 "$SI" LOW
out=$(sh "$OP" load); has "refuses an image with PA on by default" "$out" "BLOCKED: Price Action Lab is not off by default"
make_packet poll "$REAL_FLAGS" 100:101 'fetch("/research/price-action/paper")' LOW
out=$(sh "$OP" load); has "refuses a dashboard still calling the PA lab" "$out" "BLOCKED: dashboard still calls the PA lab"
make_packet root "$REAL_FLAGS" 0 "$SI" LOW
out=$(sh "$OP" load); has "refuses an image running as root" "$out" "BLOCKED: image runs as root"
out=$(PROD_SI_TEST_COMMIT=3333333333333333333333333333333333333333 sh "$OP" load); has "refuses a package sealed for another commit" "$out" "BLOCKED: package is sealed for a different commit"
make_packet nosi "$REAL_FLAGS" 100:101 'console.log("smc")' LOW
out=$(sh "$OP" load); has "refuses a dashboard without Strategy Intelligence" "$out" "BLOCKED: the dashboard has no Strategy Intelligence page"
make_packet otheruser "$REAL_FLAGS" 1000:1000 "$SI" LOW
out=$(sh "$OP" load); has "refuses an image whose user cannot own production's files" "$out" "but production's data files belong to 100:101"
out=$(PROD_SI_TEST_ZIP_SHA256=PENDING_RELEASE_14 sh "$OP" load); has "refuses while the expected SHA-256 is not filled in" "$out" "BLOCKED: the expected package SHA-256 is not filled in yet"
out=$(sh "$OP" unload); has "rejects unknown argument" "$out" "usage:"

docker rmi -f "nexus-ci-api:$COMMIT" >/dev/null 2>&1
rm -rf "$T"
echo "prod_si_images_test: $FAILS failed"
[ "$FAILS" = 0 ]
