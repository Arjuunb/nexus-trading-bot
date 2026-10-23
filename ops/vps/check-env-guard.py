"""Used by .github/workflows/vps-ops.yml before a run may skip the owner's approval.

Exit 0 only if the GitHub environment "vps-check" exists and may be used ONLY from the branch
"main". Otherwise exit 1 with the reason, and the workflow falls back to the
"vps" environment (owner approval). Reads settings only; needs GH_TOKEN and GITHUB_REPOSITORY.
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


def get(path):
    req = urllib.request.Request(API + "/repos/" + os.environ["GITHUB_REPOSITORY"] + path,
                                 headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main():
    try:
        env = get("/environments/vps-check")
        policy = env.get("deployment_branch_policy") or {}
        if policy.get("custom_branch_policies") is not True or policy.get("protected_branches") is not False:
            return "vps-check is not limited to selected branches"
        rows = get("/environments/vps-check/deployment-branch-policies").get("branch_policies") or []
        names = sorted((r.get("type", "branch"), r.get("name")) for r in rows)
        if names != [("branch", "main")]:
            listed = ", ".join("%s %s" % n for n in names) or "no branch"
            return "vps-check may be used from %s, not only from main" % listed
    except urllib.error.HTTPError as e:
        return "cannot read the vps-check environment settings (HTTP %d)" % e.code
    except Exception as e:  # noqa: BLE001 - any doubt means: ask the owner
        return "cannot read the vps-check environment settings (%s)" % type(e).__name__
    return None


if __name__ == "__main__":
    reason = main()
    if reason:
        print("approval needed: " + reason)
        sys.exit(1)
    print("vps-check is limited to main")
