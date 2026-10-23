#!/usr/bin/env python3
"""Turn a P0 evidence JSON (from collect_p0_evidence.py) into a Markdown report.

Runs locally, inside a clone of the repository (needed only to match the running
source fingerprints to commits):

    python3 analyze_p0_evidence.py nexus-p0-evidence-<stamp>.json --repo ../.. > report.md
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone

GIB = 2**30
MIB = 2**20


def fmt_bytes(n):
    if n is None:
        return "—"
    n = float(n)
    for unit, size in (("GiB", GIB), ("MiB", MIB), ("KiB", 1024)):
        if abs(n) >= size:
            return f"{n / size:.2f} {unit}"
    return f"{int(n)} B"


def parse_time(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")[:32])
    except ValueError:
        return None


# ------------------------------------------------------------ revisions ----
def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True).stdout


def match_revision(blobs: dict, repo: str):
    """Commits whose tree has exactly these blobs (None = file absent)."""
    paths = sorted(p for p, v in blobs.items() if v is None or re.fullmatch(r"[0-9a-f]{40}", str(v)))
    commits = _git(repo, "rev-list", "--all").split()
    exact, scored = [], []
    for commit in commits:
        listing = _git(repo, "ls-tree", "-r", commit, "--", *paths)
        tree = {}
        for line in listing.splitlines():
            meta, path = line.split("\t", 1)
            tree[path] = meta.split()[2]
        score = sum(1 for p in paths if tree.get(p) == blobs[p])
        if score == len(paths):
            exact.append(commit)
        scored.append((score, commit))
    describe = lambda c: _git(repo, "log", "-1", "--format=%h %cI %s", c).strip()
    scored.sort(reverse=True)
    return {"files_compared": len(paths), "exact": [describe(c) for c in exact],
            "best": [f"{s}/{len(paths)} {describe(c)}" for s, c in scored[:3]]}


def lineage(repo, commit_line):
    sha = commit_line.split()[0]
    out = {}
    for name, ref in (("guardian", "origin/codex/guardian-vps-integration"), ("main", "origin/main"),
                      ("pa_retirement", "origin/fix/pa-retirement-recovery-options-20261022")):
        r = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", sha, ref], capture_output=True)
        out[name] = r.returncode == 0
    return out


# ------------------------------------------------------------- sections ----
def section_identity(ev, repo, md, verdicts):
    md.append("## 1. Production identity and rollback images (B1, B2)\n")
    conts = ev.get("containers") or []
    if isinstance(conts, dict):
        md.append(f"Container inventory failed: `{conts.get('error')}`\n")
        return
    md.append("| Container | Status | Image | Started | Restarts | OOM | Memory limit | GIT_COMMIT env |")
    md.append("|---|---|---|---|---|---|---|---|")
    for c in conts:
        md.append(f"| {c['name']} | {c['status']} | `{(c.get('image_id') or '')[7:19]}` {c.get('image_ref') or ''} | "
                  f"{(c.get('started_at') or '')[:19]} | {c.get('restart_count')} | {c.get('oom_killed')} | "
                  f"{fmt_bytes(c.get('memory_limit')) if c.get('memory_limit') else 'unlimited'} | "
                  f"{(c['env']['values'].get('GIT_COMMIT') or '—')[:12]} |")
    md.append("")
    srcs = ev.get("running_source") or {}
    b1 = "UNRESOLVED"
    for name, src in srcs.items():
        if "blobs" not in src:
            md.append(f"- **{name}**: source fingerprint unavailable: {src.get('error')}")
            continue
        if repo:
            m = match_revision(src["blobs"], repo)
            md.append(f"- **{name}** running source ({m['files_compared']} files fingerprinted):")
            if m["exact"]:
                md.append(f"  - Exact match: {'; '.join(m['exact'][:5])}" + (" (+more)" if len(m["exact"]) > 5 else ""))
                lin = lineage(repo, m["exact"][0])
                md.append(f"  - Lineage: Guardian={lin['guardian']}, main={lin['main']}, PA-retirement branch={lin['pa_retirement']}")
                if "app-1" in name or name.endswith("-app"):
                    b1 = "RESOLVED"
            else:
                md.append(f"  - No exact commit. Closest: {'; '.join(m['best'])}")
                if "app" in name:
                    b1 = "PARTIAL (local modifications or unpublished commit)"
        absent = [p for p, v in src["blobs"].items() if v is None]
        if absent:
            md.append(f"  - Absent in image: {', '.join(absent)}")
    perf = ev.get("app_performance") or {}
    for key, h in perf.items():
        if key.startswith("health:") and isinstance(h, dict) and h.get("deploy_info"):
            md.append(f"- `/health` deploy info for {key[7:]}: `{json.dumps(h['deploy_info'])[:300]}`")
    for path, st in (ev.get("checkouts") or {}).items():
        if isinstance(st, dict):
            md.append(f"- Checkout `{path}`: head `{str(st.get('head', ''))[:12]}` branch `{st.get('branch', '')}`, "
                      f"modified tracked files: {st.get('modified_tracked_files', '—')}, upstream `{st.get('upstream', '')}`")
    md.append("")
    imgs = (ev.get("images") or {}).get("inspected") or []
    used = {c.get("image_id") for c in conts}
    app_imgs = [i for i in imgs if any("nexus-trading-bot-app" in (t or "") for t in (i.get("repo_tags") or []))]
    md.append("**Images for the production app repository** (newest first):\n")
    md.append("| Tags | Image | Created | Size | GIT_COMMIT | In use |")
    md.append("|---|---|---|---|---|---|")
    for i in sorted(app_imgs, key=lambda x: x.get("created") or "", reverse=True):
        md.append(f"| {', '.join(i.get('repo_tags') or [])[:90]} | `{(i.get('id') or '')[7:19]}` | {(i.get('created') or '')[:19]} | "
                  f"{fmt_bytes(i.get('size'))} | {(i.get('env') or {}).get('GIT_COMMIT', '—')[:12]} | {i.get('id') in used} |")
    rollback = [i for i in app_imgs if i.get("id") not in used]
    b2 = "RESOLVED: rollback image(s) retained" if rollback else "RISK: no earlier production image retained"
    if not app_imgs:
        b2 = "UNRESOLVED: no image tagged nexus-trading-bot-app found"
    md.append("")
    verdicts["B1"] = b1
    verdicts["B2"] = b2


def section_disk(ev, md, verdicts):
    md.append("## 2. Disk usage and the free-space drop (B5)\n")
    d = ev.get("disk") or {}
    for line in (d.get("df") or {}).get("bytes", [])[:12]:
        md.append(f"    {line}")
    md.append("")
    sdf = d.get("docker_system_df")
    if isinstance(sdf, list):
        md.append("| Docker storage | Total | Size | Reclaimable |")
        md.append("|---|---|---|---|")
        for row in sdf:
            md.append(f"| {row.get('Type')} | {row.get('TotalCount')} | {row.get('Size')} | {row.get('Reclaimable')} |")
        md.append("")
    walks = d.get("walks") or {}
    md.append("| Location | Allocated | Files | Truncated |")
    md.append("|---|---|---|---|")
    for label, w in sorted(walks.items(), key=lambda kv: -(kv[1].get("allocated_bytes") or 0)):
        md.append(f"| {label} | {fmt_bytes(w.get('allocated_bytes'))} | {w.get('files', '—')} | {w.get('truncated') or w.get('skipped') or ''} |")
    md.append("")
    by_day = defaultdict(int)
    largest = []
    for label, w in walks.items():
        for day, n in (w.get("allocated_by_mtime_day") or {}).items():
            by_day[day] += n
        for f in w.get("largest_files") or []:
            largest.append((f["allocated"], label, f))
    md.append("**Bytes by last-modified day (all walked locations)** — files written or rewritten that day:\n")
    md.append("| Day | Allocated |")
    md.append("|---|---|")
    for day in sorted(by_day)[-14:]:
        md.append(f"| {day} | {fmt_bytes(by_day[day])} |")
    md.append("")
    md.append("**Largest files**\n")
    md.append("| Allocated | Location | Path | Modified | Kind |")
    md.append("|---|---|---|---|---|")
    for alloc, label, f in sorted(largest, key=lambda x: -x[0])[:25]:
        md.append(f"| {fmt_bytes(alloc)} | {label} | `{f['path'][:90]}` | {f['mtime'][:16]} | {f['kind']} |")
    md.append("")
    has_volumes = any(k.startswith("volume:") and "allocated_bytes" in w for k, w in walks.items())
    verdicts["B5"] = ("RESOLVED (attribution below)" if has_volumes and isinstance(sdf, list)
                      else "PARTIAL (host directories only; Docker volumes/images not measured)" if walks else "UNRESOLVED")


def section_databases(ev, md, verdicts):
    md.append("## 3. Database sizes and Price Action table attribution (B4)\n")
    dbs = ev.get("databases") or {}
    b4 = "UNRESOLVED"
    for env, info in dbs.items():
        if not isinstance(info, dict) or "files" not in info:
            continue
        md.append(f"### {env} (`{info.get('data_dir')}`)\n")
        md.append("| Database | File | WAL | Free pages | Header consistent | Open FDs |")
        md.append("|---|---|---|---|---|---|")
        fds = info.get("open_fds") or {}
        for name, f in sorted(info["files"].items(), key=lambda kv: -((kv[1].get("header") or {}).get("size") or 0)):
            h = f.get("header") or {}
            wal = (f.get("sidecars") or {}).get("-wal", {}).get("size")
            fd = sum(n for k, n in fds.items() if k.split("-")[0] == name or k == name)
            md.append(f"| {name} | {fmt_bytes(h.get('size'))} | {fmt_bytes(wal)} | {fmt_bytes(h.get('freelist_bytes'))} | "
                      f"{h.get('header_size_matches_file')} | {fd} |")
        md.append("")
        for name, f in info["files"].items():
            if "price_action" not in name and (f.get("header") or {}).get("size", 0) < 256 * MIB:
                continue
            ins = f.get("inspection") or {}
            tables = ins.get("tables") or {}
            if not tables:
                md.append(f"- {name}: not inspected ({ins.get('error') or f.get('reason')})")
                continue
            size = (f.get("header") or {}).get("size") or 1
            exact = ins.get("dbstat_exact")
            exact = exact if isinstance(exact, dict) and exact and "error" not in exact else None
            if exact:
                # Exact page accounting replaces the sampled estimate where the file was small enough.
                for t, row in tables.items():
                    if isinstance(row.get("rows_est"), int):
                        row["est_bytes"] = exact.get(t, row.get("est_bytes") or 0)
                        if isinstance(row.get("est_index_bytes"), dict):
                            row["est_index_bytes"] = {n: exact.get(n, v) for n, v in row["est_index_bytes"].items()}
            idx_of = lambda t: sum((t.get("est_index_bytes") or {}).values()) if isinstance(t.get("est_index_bytes"), dict) else 0
            est_total = sum((t.get("est_bytes") or 0) + idx_of(t) for t in tables.values())
            freelist = (f.get("header") or {}).get("freelist_bytes") or 0
            failed = [t for t, r in tables.items() if r.get("error")]
            md.append(f"**{name}** — {fmt_bytes(size)}; {'exact' if exact else 'estimated'} table+index size "
                      f"{fmt_bytes(est_total)} ({100 * est_total / size:.0f}% of file), free pages {fmt_bytes(freelist)}\n")
            remainder = size - freelist - est_total
            if failed and remainder > 0.1 * size:
                md.append(f"Not sampled ({', '.join(failed)}): the remaining **{fmt_bytes(remainder)} "
                          f"({100 * remainder / size:.0f}% of the file)** belongs to it by subtraction "
                          "(file size − free pages − every other table and index).\n")
            md.append("| Table | Rows (est.) | Avg row | Est. table | Est. indexes | Share of file | Sample categories |")
            md.append("|---|---|---|---|---|---|---|")
            for t, row in sorted(tables.items(), key=lambda kv: -((kv[1].get("est_bytes") or 0) + idx_of(kv[1])))[:12]:
                cats = row.get("sample_categories") or {}
                cat_text = ", ".join(f"{k}: {v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])[:4])
                if not isinstance(row.get("rows_est"), int):
                    md.append(f"| {t} | {row.get('skipped') or row.get('error')} ||||||")
                    continue
                both = (row.get("est_bytes") or 0) + idx_of(row)
                md.append(f"| {t} | {row['rows_est']:,} | {fmt_bytes(row.get('avg_row_payload_bytes'))} | "
                          f"{fmt_bytes(row.get('est_bytes'))} | {fmt_bytes(idx_of(row))} | {100 * both / size:.1f}% | {cat_text} |")
            if exact:
                md.append(f"\nSizes above are exact page accounting ({len(exact)} b-trees).")
            rev = tables.get("pa_journal_revisions") or {}
            if rev.get("rowid_time_samples"):
                md.append("\n`pa_journal_revisions` growth (rowid → created_at samples): " +
                          ", ".join(f"{r}@{t[:10]}" for r, t in rev["rowid_time_samples"][:12]))
            md.append("")
            if "price_action_paper" in name:
                b4 = "RESOLVED (estimated, method validated locally)"
    verdicts["B4"] = b4


def section_resources(ev, md, verdicts):
    md.append("## 4. CPU, memory and disk I/O samples (B3)\n")
    r = ev.get("resources") or {}
    if "samples" not in r:
        md.append(f"Not collected: {r.get('error', 'skipped')}\n")
        verdicts["B3"] = "UNRESOLVED"
        return
    hc = r.get("host_cpu_percent") or {}
    md.append(f"Window {r['window_s']} s, {r['samples']} samples, {r.get('nproc')} CPUs. Host CPU busy avg "
              f"{(hc.get('busy') or {}).get('avg')}% (p95 {(hc.get('busy') or {}).get('p95')}%), iowait avg "
              f"{(hc.get('iowait') or {}).get('avg')}%, steal avg {(hc.get('steal') or {}).get('avg')}%. "
              f"MemAvailable min {(r.get('host_mem_available_mib') or {}).get('p50')} MiB (p50). Swap {r.get('host_swap_total_mib')} MiB. "
              f"PSI: {json.dumps(r.get('psi_last'))}\n")
    md.append("| Container | CPU % of one core (avg / p95 / max) | Memory MiB (avg / max) | Peak MiB | Read MiB/s | Write MiB/s | OOM kills |")
    md.append("|---|---|---|---|---|---|---|")
    for name, c in sorted((r.get("containers") or {}).items()):
        cpu, mem = c.get("cpu_percent_of_one_core") or {}, c.get("mem_current_mib") or {}
        md.append(f"| {name} | {cpu.get('avg')} / {cpu.get('p95')} / {cpu.get('max')} | {mem.get('avg')} / {mem.get('max')} | "
                  f"{c.get('mem_peak_mib')} | {c.get('read_mib_s')} | {c.get('write_mib_s')} | {c.get('oom_kill_total')} |")
    md.append("")
    for dev, io in (r.get("disk_io") or {}).items():
        md.append(f"- Disk `{dev}`: read {io['read_mib_s']} MiB/s, write {io['write_mib_s']} MiB/s, util {io['util_percent']}%")
    th = r.get("main_app_threads") or {}
    md.append(f"- Production app threads: {th.get('count')}; CPU share of busiest threads: "
              f"{', '.join(str(t['share']) + '%' for t in (th.get('top_thread_share_percent') or [])[:5])}")
    md.append("")
    verdicts["B3"] = ("RESOLVED (one sampling window; repeat at a busier time for a range)" if r.get("containers")
                      else "PARTIAL (host only; no container cgroups sampled)")


def section_app(ev, md):
    md.append("## 5. Application performance samples\n")
    perf = ev.get("app_performance") or {}
    for key, v in perf.items():
        if key.startswith("access_log:") and isinstance(v, dict) and "requests" in v:
            md.append(f"**{key[11:]}** — {v['requests']} requests in {v['window_minutes']} min "
                      f"({v['requests_per_minute']}/min); status classes {v['status_classes']}\n")
            md.append("| Path | Requests | Bytes | Statuses |")
            md.append("|---|---|---|---|")
            for p in v["top_paths"][:15]:
                md.append(f"| `{p['path']}` | {p['count']} | {fmt_bytes(p['bytes'])} | {p['status']} |")
            if v.get("error_paths"):
                md.append(f"\nError responses by path: `{json.dumps(v['error_paths'])[:600]}`")
            md.append("")
        elif key.startswith("health:") and isinstance(v, dict):
            md.append(f"- `/health` {key[7:]}: {v.get('samples_ms')} ms, codes {v.get('codes')}")
            ps = v.get("public_status") or {}
            if ps.get("components"):
                md.append(f"- `/status/public` {key[7:]}: overall **{ps.get('overall')}**, monitoring since "
                          f"{ps.get('monitoring_since')}, {ps.get('incident_count')} incidents in 90 days")
                for comp in ps["components"]:
                    md.append(f"  - {comp['id']}: {comp['state']} (90-day uptime {comp.get('uptime_pct')}%) — {comp.get('detail')}")
                for inc in (ps.get("incidents") or [])[:8]:
                    md.append(f"  - incident {inc.get('component')} {inc.get('state')} {str(inc.get('started_at'))[:16]} "
                              f"for {inc.get('duration_min')} min{' (ongoing)' if inc.get('ongoing') else ''}: {inc.get('detail')}")
        elif key.startswith("log_signals_24h:") and isinstance(v, dict):
            md.append(f"- Log signals (24 h) {key[16:]}: {v.get('counts')} over {v.get('log_lines_24h')} lines")
    md.append("")
    for env, info in (ev.get("databases") or {}).items():
        for name, f in (info.get("files") or {}).items() if isinstance(info, dict) else []:
            app = (f.get("inspection") or {}).get("app") or {}
            if app.get("guardian_meta"):
                md.append(f"- {env} Guardian heartbeat: {app['guardian_meta'].get('heartbeat')}")
            if app.get("journal_recorder"):
                jr = app["journal_recorder"]
                md.append(f"- {env} Journal Recorder last pass: {jr.get('last_reconcile_seconds')} s at "
                          f"{jr.get('last_reconcile_at')}, errors {jr.get('last_reconcile_errors')}")
    md.append("")


def section_backups(ev, md, verdicts):
    md.append("## 6. Backups, WAL state and restore readiness (B8)\n")
    off_host = (ev.get("schedules") or {}).get("off_host_tool_detected")
    for label, b in (ev.get("backups") or {}).items():
        if not isinstance(b, dict) or not b.get("exists"):
            md.append(f"- {label}: no backup directory at `{(b or {}).get('path')}`")
            continue
        entries = b.get("entries") or []
        bad = [e["name"] for e in entries for f, chk in (e.get("db_header_checks") or {}).items()
               if chk.get("header_size_matches_file") is False]
        errs = [e["name"] for e in entries if (e.get("manifest") or {}).get("errors")]
        md.append(f"- **{label}** `{b['path']}`: {b.get('snapshot_count')} snapshots, {fmt_bytes(b.get('total_allocated'))}; "
                  f"newest {entries[-1]['name'] if entries else '—'}; snapshots with errors: {errs or 'none'}; "
                  f"truncated database copies: {bad or 'none'}")
    for name, r in (ev.get("restore_readiness") or {}).items():
        md.append(f"- {name}: restore_to={r.get('has_restore_to')}, restore_check={r.get('has_restore_check')}, "
                  f"sealed archives supported={r.get('supports_sealed_archives')}, KEEP={r.get('keep_default')}, "
                  f"master key configured={r.get('master_key_configured')}")
    md.append(f"- Off-host backup tool detected in schedules: {off_host}")
    md.append("")
    found = any(isinstance(b, dict) and b.get("exists") for b in (ev.get("backups") or {}).values())
    verdicts["B8"] = ("UNRESOLVED (no backup directory inspected)" if not found else
                      "CHECK: an off-host copy tool is scheduled" if off_host else
                      "CONFIRMED RISK: backups exist only on this host; no off-host schedule found")


def section_dependencies(ev, md, verdicts):
    md.append("## 7. Protected dependencies (Guardian, SMC Agent, Adaptive MTF, instances, journals) (B6, B7)\n")
    sessions = []
    for env, info in (ev.get("databases") or {}).items():
        if not isinstance(info, dict):
            continue
        for name, f in (info.get("files") or {}).items():
            app = (f.get("inspection") or {}).get("app") or {}
            if app.get("trading_instances"):
                md.append(f"**{env} / {name} — trading instances**\n")
                md.append("| Id | Strategy | Symbol | TF | Mode | State | Desired running | Updated |")
                md.append("|---|---|---|---|---|---|---|---|")
                for i in app["trading_instances"]:
                    md.append(f"| {str(i.get('id'))[:12]} | {i.get('strategy_label') or i.get('strategy_key')} | {i.get('symbol')} | "
                              f"{i.get('timeframe')} | {i.get('mode')} | {i.get('state')} | {i.get('desired_running')} | {str(i.get('updated_at'))[:16]} |")
                md.append("")
            for t in ("pa_sessions", "smc_sessions"):
                for s in app.get(t) or []:
                    copy = any(k in name for k in ("deployment-backups/", "validation/", "backups/"))
                    sessions.append((env, t, s, name, copy))
            if app.get("latest_row_time"):
                md.append(f"- {env} / {name} latest checkpointed rows: `{json.dumps(app['latest_row_time'])[:400]}`")
            if app.get("trade_records_by_source"):
                md.append(f"- {env} journal records by source/status: `{json.dumps(app['trade_records_by_source'])[:500]}`")
    if sessions:
        md.append("\n| Env | Lab | Session | Mode | Operating mode | Symbol | TF | Status | Started | Source file |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for env, t, s, name, copy in sorted(sessions, key=lambda x: (x[4], x[0], x[1])):
            md.append(f"| {env} | {t[:-9].upper()} | {str(s.get('id'))[:10]} | {s.get('mode')} | {s.get('operating_mode')} | "
                      f"{s.get('symbol')} | {s.get('timeframe')} | {s.get('status')} | {str(s.get('started_at'))[:16]} | "
                      f"{name}{' (copy, not live)' if copy else ''} |")
    md.append("")
    verdicts["B6"] = "RESOLVED" if any(not x[4] for x in sessions) else "UNRESOLVED"
    backend = None
    for key, v in (ev.get("app_performance") or {}).items():
        if key.startswith("startup_log:") and isinstance(v, dict):
            for line in v.get("lines") or []:
                m = re.search(r"ledger backend = (\w+)", line)
                if m:
                    backend = m.group(1)
                    md.append(f"- {key[12:]}: `{line}`")
    verdicts["B7"] = f"RESOLVED: {backend}" if backend else "UNRESOLVED (startup line not in retained logs)"
    md.append("")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("evidence")
    ap.add_argument("--repo", default=None, help="git clone used to match source fingerprints")
    opts = ap.parse_args(argv)
    with open(opts.evidence) as fh:
        ev = json.load(fh)
    md, verdicts = [], {}
    md.append(f"# Nexus P0 VPS evidence — {ev.get('host')} — {ev.get('started_at')} → {ev.get('finished_at')}\n")
    md.append(f"Schema `{ev.get('schema')}`. Guarantees declared by the collector: `{json.dumps(ev.get('guarantees'))}`\n")
    section_identity(ev, opts.repo, md, verdicts)
    section_disk(ev, md, verdicts)
    section_databases(ev, md, verdicts)
    section_resources(ev, md, verdicts)
    section_app(ev, md)
    section_backups(ev, md, verdicts)
    section_dependencies(ev, md, verdicts)
    md.append("## Blocker status\n")
    md.append("| Blocker | Status |")
    md.append("|---|---|")
    for k in ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8"):
        md.append(f"| {k} | {verdicts.get(k, 'UNRESOLVED')} |")
    steps = ev.get("_step_seconds") or {}
    md.append(f"\nCollector step durations (s): `{json.dumps(steps)}`")
    failed = {k: v["error"] for k, v in ev.items() if isinstance(v, dict) and "error" in v}
    if failed:
        md.append(f"\nSteps that failed: `{json.dumps(failed)[:800]}`")
    print("\n".join(md))


if __name__ == "__main__":
    main()
