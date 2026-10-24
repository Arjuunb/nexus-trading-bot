#!/bin/sh
# Check and load the scanned Strategy Intelligence API image for PRODUCTION (commit 14adf5d on
# codex/guardian-vps-integration: PR #48, plus the release fixes from PRs #49 and #50) on the VPS.
#
#   load   1. the uploaded package must have exactly the SHA-256 GitHub recorded for the
#             unpublished-staging-images artifact of release run 40072349764 (release #16)
#          2. unpack it into /var/lib/nexus-ops/si-production/packet and check the sealed file list:
#             every file's size and SHA-256, no extra files, the reviewed commit and source fingerprint
#          3. read the security reports in the package: 0 HIGH/CRITICAL vulnerabilities, 0 secrets
#          4. check the API image archive itself: config fingerprint, revision and source labels,
#             linux/amd64, and that build-proof.json names an image that is in this archive
#          5. docker load the API image only (the worker image is not needed) and check the loaded
#             image is that same image (ID, layers, labels)
#          6. run the image once with no network, read-only, without starting the app: Python
#             version, Price Action Lab switch default (must be off), dashboard has no PA lab calls
#          7. record the image ID for the deploy step in api-image.json
#
# It adds one image to Docker and files under /var/lib/nexus-ops/si-production. It does not start,
# stop or change any container, and does not touch any data.
set -u
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in load) ;; *) echo "usage: prod-si-images.sh load"; exit 64 ;; esac
exec python3 -I - "$1" <<'PY'
import gzip, hashlib, io, json, os, re, shutil, subprocess, sys, tarfile, time, zipfile

ZIP = os.environ.get("PROD_SI_TEST_ZIP", "/home/ubuntu/nexus-si-production/unpublished-staging-images-40072349764-1.zip")
# GitHub's recorded SHA-256 of the artifact zip; filled in from release #16 when it finishes.
ZIP_SHA256 = os.environ.get("PROD_SI_TEST_ZIP_SHA256", "PENDING_RELEASE_16")
COMMIT = os.environ.get("PROD_SI_TEST_COMMIT", "14adf5d17e6b9fd7d7e587fd99bb633f507b187e")
SOURCE = os.environ.get("PROD_SI_TEST_SOURCE", "96b52ebb6947e6438e0c8515c344e9c02a6610b718e09f778b6465c56383667d")
WORK = os.environ.get("PROD_SI_TEST_WORK", "/var/lib/nexus-ops/si-production")
PACKET = os.path.join(WORK, "packet")
TAG = "nexus-ci-api:" + COMMIT
HEX64 = re.compile(r"[0-9a-f]{64}")

class Blocked(Exception): pass

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""): h.update(b)
    return h.hexdigest()

def run(cmd, timeout=600, input=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
    if r.returncode: raise Blocked("%s failed: %s" % (" ".join(cmd[:3]), (r.stderr.strip() or r.stdout.strip())[-200:]))
    return r.stdout

def say(msg): print(msg, flush=True)

def safe_name(name):
    parts = name.split("/")
    return name and not name.startswith("/") and "\\" not in name and ".." not in parts and "" not in parts[:-1]

def main():
    # 1. the uploaded package
    if not HEX64.fullmatch(ZIP_SHA256): raise Blocked("the expected package SHA-256 is not filled in yet")
    if not os.path.isfile(ZIP) or os.path.islink(ZIP): raise Blocked("package not found at %s" % ZIP)
    size = os.path.getsize(ZIP)
    if size > 1_000_000_000: raise Blocked("package is larger than 1 GB")
    got = sha256(ZIP)
    if got != ZIP_SHA256: raise Blocked("package SHA-256 %s... is not the one GitHub recorded (%s...)" % (got[:16], ZIP_SHA256[:16]))
    say("1. package %s (%.0f MB) has GitHub's recorded SHA-256 %s..." % (os.path.basename(ZIP), size / 1e6, got[:16]))

    # 2. unpack and check the sealed file list
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    tmp = PACKET + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, mode=0o700)
    with zipfile.ZipFile(ZIP) as z:
        infos = z.infolist()
        if len(infos) > 1000 or sum(i.file_size for i in infos) > 8 * 1024**3: raise Blocked("package too large when unpacked")
        for i in infos:
            if not safe_name(i.filename.rstrip("/")) or (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise Blocked("unsafe path or link in package: %r" % i.filename[:80])
        z.extractall(tmp)
    seal = json.load(open(os.path.join(tmp, "packet-integrity.json")))
    if seal.get("commit") != COMMIT or seal.get("source_sha256") != SOURCE: raise Blocked("package is sealed for a different commit or source")
    seen = set()
    for row in seal.get("files", []):
        name = row["path"]; p = os.path.join(tmp, name)
        if not safe_name(name) or name in seen or os.path.islink(p) or not os.path.isfile(p): raise Blocked("sealed file missing or unsafe: %s" % name)
        if os.path.getsize(p) != row["bytes"] or sha256(p) != row["sha256"]: raise Blocked("sealed file changed: %s" % name)
        seen.add(name)
    actual = {os.path.relpath(os.path.join(d, f), tmp) for d, _, fs in os.walk(tmp) for f in fs} - {"packet-integrity.json"}
    if actual != seen: raise Blocked("package has files that are not in its seal: %s" % sorted(actual - seen)[:3])
    source = json.load(open(os.path.join(tmp, "source-manifest.json")))
    if source.get("head") != COMMIT or source.get("source_sha256") != SOURCE: raise Blocked("source manifest is for a different commit")
    shutil.rmtree(PACKET, ignore_errors=True)
    os.replace(tmp, PACKET)
    say("2. sealed file list verified: %d files, commit %s, source %s..." % (len(seen), COMMIT[:12], SOURCE[:16]))

    # 3. security reports
    def findings(name, kinds):
        data = json.load(open(os.path.join(PACKET, name)))
        n = 0
        for r in data.get("Results") or []:
            for k in kinds:
                for item in r.get(k) or []:
                    if k == "Vulnerabilities" and item.get("Severity") not in ("HIGH", "CRITICAL"): continue
                    n += 1
        return n
    vul = findings("api-security.json", ("Vulnerabilities", "Secrets"))
    sec = findings("api-all-secrets.json", ("Secrets",))
    if vul or sec: raise Blocked("security reports list %d HIGH/CRITICAL findings and %d secrets" % (vul, sec))
    say("3. security reports in the package: 0 HIGH/CRITICAL vulnerabilities, 0 secrets in the API image")

    # 4. the API image archive
    proof = json.load(open(os.path.join(PACKET, "build-proof.json")))
    if proof.get("commit") != COMMIT or proof.get("source_sha256") != SOURCE: raise Blocked("build proof is for a different commit")
    proof_id = proof["images"]["api"]["config_id"]
    blobs, manifest_json, index_json = {}, None, None
    with tarfile.open(os.path.join(PACKET, "api.tar.gz"), "r:gz") as t:
        for m in t:
            if not m.isfile(): continue
            if m.name == "manifest.json": manifest_json = json.load(t.extractfile(m))
            elif m.name == "index.json": index_json = json.load(t.extractfile(m))
            elif m.name.startswith("blobs/sha256/") and m.size < 4 << 20:
                blobs[m.name.rsplit("/", 1)[1]] = t.extractfile(m).read()
    if not manifest_json or len(manifest_json) != 1: raise Blocked("API archive must hold exactly one image")
    cfg_name = manifest_json[0]["Config"].rsplit("/", 1)[1].removesuffix(".json")
    cfg_raw = blobs.get(cfg_name)
    if cfg_raw is None or hashlib.sha256(cfg_raw).hexdigest() != cfg_name: raise Blocked("API image config does not match its fingerprint")
    cfg = json.loads(cfg_raw)
    labels = (cfg.get("config") or {}).get("Labels") or {}
    if cfg.get("os") != "linux" or cfg.get("architecture") != "amd64" or labels.get("org.opencontainers.image.revision") != COMMIT \
            or labels.get("nexus.source.sha256") != SOURCE:
        raise Blocked("API image is not linux/amd64 for commit %s" % COMMIT[:12])
    accepted = {"sha256:" + cfg_name}
    queue = [d["digest"] for d in (index_json or {}).get("manifests", [])]
    while queue:
        dg = queue.pop(); accepted.add(dg)
        raw = blobs.get(dg.split(":", 1)[1])
        if raw is not None and hashlib.sha256(raw).hexdigest() == dg.split(":", 1)[1]:
            doc = json.loads(raw)
            queue += [d["digest"] for d in doc.get("manifests", [])]
    if proof_id not in accepted: raise Blocked("build proof names an image that is not in this archive")
    diff_ids = cfg["rootfs"]["diff_ids"]
    say("4. API image archive: config sha256:%s..., linux/amd64, revision %s, %d layers; build proof matches" % (cfg_name[:16], COMMIT[:12], len(diff_ids)))

    # 5. load and compare
    out = run(["docker", "load", "-q", "-i", os.path.join(PACKET, "api.tar.gz")], timeout=900)
    img = json.loads(run(["docker", "image", "inspect", TAG]))[0]
    ilabels = (img.get("Config") or {}).get("Labels") or {}
    if img["Id"] not in accepted: raise Blocked("loaded image ID %s is not from the archive" % img["Id"][:19])
    if img["RootFS"].get("Layers") != diff_ids or ilabels.get("org.opencontainers.image.revision") != COMMIT \
            or ilabels.get("nexus.source.sha256") != SOURCE or img.get("Architecture") != "amd64":
        raise Blocked("loaded image layers or labels differ from the archive")
    say("5. loaded %s as %s (layers and labels identical to the archive)" % (TAG[:24] + "...", img["Id"]))

    # 6. probe without starting the app
    code = ("import json,os,sys,pathlib; sys.path.insert(0,'/app/automation-hub'); os.environ.pop('PRICE_ACTION_LAB_ENABLED',None);"
            "from services.feature_flags import price_action_lab_enabled as f;"
            "root=pathlib.Path('/app/automation-hub/webui');"
            "hits=sum(1 for p in root.rglob('*') if p.is_file() and p.suffix in ('.js','.html') and b'research/price-action' in p.read_bytes());"
            "si=sum(1 for p in root.rglob('*') if p.is_file() and p.suffix=='.js' and b'strategy-intelligence' in p.read_bytes());"
            "print(json.dumps({'python':sys.version.split()[0],'pa_lab_default':f(),'dashboard_files_calling_pa_lab':hits,"
            "'webui_present':(root/'index.html').is_file(),'dashboard_files_with_strategy_intelligence':si,'uid':os.getuid(),'gid':os.getgid()}))")
    probe = json.loads(run(["docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only", "--cpus", "0.5",
                            "--memory", "256m", "--pids-limit", "64", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                            "--entrypoint", "python", img["Id"], "-B", "-c", code], timeout=120).strip().splitlines()[-1])
    if probe["pa_lab_default"] is not False: raise Blocked("Price Action Lab is not off by default in this image")
    if probe["dashboard_files_calling_pa_lab"] or not probe["webui_present"]: raise Blocked("dashboard still calls the PA lab or is missing")
    if probe["uid"] == 0: raise Blocked("image runs as root")
    if not probe["dashboard_files_with_strategy_intelligence"]: raise Blocked("the dashboard has no Strategy Intelligence page")
    if (probe["uid"], probe["gid"]) != (100, 101):
        raise Blocked("image runs as %d:%d, but production's data files belong to 100:101" % (probe["uid"], probe["gid"]))
    say("6. image probe (no network, app not started): Python %s, runs as %d:%d like production's data files, Price Action Lab default OFF, "
        "dashboard has 0 files calling the PA lab and includes Strategy Intelligence" % (probe["python"], probe["uid"], probe["gid"]))

    # 7. record
    record = {"image_id": img["Id"], "tag": TAG, "config_digest": "sha256:" + cfg_name, "commit": COMMIT, "source_sha256": SOURCE,
              "package_sha256": ZIP_SHA256, "build_proof_id": proof_id, "layers": len(diff_ids), "probe": probe,
              "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(os.path.join(WORK, "api-image.json.tmp"), "w") as f: json.dump(record, f, indent=1)
    os.replace(os.path.join(WORK, "api-image.json.tmp"), os.path.join(WORK, "api-image.json"))
    say("7. recorded for the deploy step: %s" % os.path.join(WORK, "api-image.json"))
    say("READY: the Strategy Intelligence image is loaded and verified. No container was started, stopped or changed.")

try:
    main()
except Blocked as e:
    print("BLOCKED: %s. No container was changed." % e); sys.exit(2)
PY
