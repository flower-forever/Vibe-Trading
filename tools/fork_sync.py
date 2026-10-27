"""Create an immutable upstream integration PR and explicitly dispatch its CI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode

OFFICIAL = "HKUDS/Vibe-Trading"
DESTINATION = "flower-forever/Vibe-Trading"
WORKFLOW = "fork-ci.yml"


def run(args: list[str], cwd: Path, *, check: bool = True, data: str | None = None):
    result = subprocess.run(args, cwd=cwd, input=data, capture_output=True, text=True, encoding="utf-8")
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def git(root: Path, *args: str, check: bool = True):
    return run(["git", *args], root, check=check)


def ancestor(root: Path, older: str, newer: str) -> bool:
    result = git(root, "merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr)
    return result.returncode == 0


def candidate_name(base: str, upstream: str) -> str:
    if not all(re.fullmatch(r"[a-f0-9]{40}", value) for value in (base, upstream)):
        raise ValueError("Expected full Git SHA values")
    return f"automation/upstream-{upstream[:12]}-{base[:12]}"


def merge_candidate(root: Path, base: str, upstream: str, branch: str) -> dict:
    """Merge in a fresh checkout; on conflict retain a PR head GitHub can explain."""
    if not branch.startswith("automation/upstream-"):
        raise ValueError("Only automation candidate branches may be created")
    git(root, "check-ref-format", "--branch", branch)
    if git(root, "status", "--porcelain").stdout.strip():
        raise RuntimeError("Refusing to modify a dirty checkout")
    git(root, "switch", "--create", branch, base)
    merged = git(root, "-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com", "merge", "--no-ff", "--no-edit", "-m", f"merge: integrate official upstream {upstream[:12]}", upstream, check=False)
    conflicts = git(root, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
    if merged.returncode:
        if not conflicts:
            raise RuntimeError("Merge failed without a normal file conflict: " + merged.stderr)
        git(root, "merge", "--abort")
        # This is a NEW automation branch in an ephemeral runner, never main.
        git(root, "switch", "-C", branch, upstream)
    return {"state": "conflicts" if conflicts else "ready", "conflicts": conflicts, "head_sha": git(root, "rev-parse", "HEAD").stdout.strip()}


class GitHub:
    def __init__(self, root: Path):
        self.root = root

    def api(self, path: str, *, method: str = "GET", body: dict | None = None):
        args = ["gh", "api", "--method", method, path]
        if body is not None:
            args += ["--input", "-"]
        raw = run(args, self.root, data=json.dumps(body) if body is not None else None).stdout
        return json.loads(raw) if raw.strip() else None


def open_pr(api: GitHub, repository: str, base: str, branch: str, result: dict, upstream: str) -> dict:
    owner = repository.split("/", 1)[0]
    query = urlencode({"state": "open", "head": f"{owner}:{branch}", "base": base})
    existing = api.api(f"repos/{repository}/pulls?{query}")
    if existing:
        return {"url": existing[0]["html_url"], "number": existing[0]["number"], "created": False}
    conflict_text = "\n".join("- " + json.dumps(name, ensure_ascii=False) for name in result["conflicts"])
    detail = ("Merge conflicts require manual resolution. CI is not run against the unmerged upstream tree.\n\n" + conflict_text) if result["state"] == "conflicts" else "The candidate merges official upstream with our current main. Fork CI is explicitly dispatched on this candidate branch. Review its checks before merging."
    body = f"<!-- managed-upstream-sync -->\nOfficial repository: {OFFICIAL}\nOfficial commit: `{upstream}`\nFork base: `{base}`\n\n{detail}\n\nThis automation never force-pushes main, overwrites existing candidate branches, or auto-merges. API keys and local runtime data are not needed."
    try:
        pr = api.api(f"repos/{repository}/pulls", method="POST", body={"base": base, "head": branch, "title": f"{'[conflicts]' if result['state'] == 'conflicts' else '[upstream]'} Sync official main {upstream[:12]}", "body": body, "draft": result["state"] == "conflicts"})
    except RuntimeError as exc:
        if "not permitted to create or approve pull requests" not in str(exc):
            raise
        # Some forks forbid bot PR creation. Keep the candidate and its CI;
        # provide GitHub's compare page without changing repository settings.
        return {"url": f"https://github.com/{repository}/compare/{base}...{branch}?expand=1", "created": False, "manual_creation_required": True}
    return {"url": pr["html_url"], "number": pr["number"], "created": True}


def dispatch_ci(api: GitHub, repository: str, branch: str) -> None:
    api.api(f"repos/{repository}/actions/workflows/{WORKFLOW}/dispatches", method="POST", body={"ref": branch})


def has_dispatched_ci(api: GitHub, repository: str, branch: str, head: str) -> bool:
    query = urlencode({"branch": branch, "event": "workflow_dispatch", "head_sha": head, "per_page": 1})
    runs = api.api(f"repos/{repository}/actions/workflows/{WORKFLOW}/runs?{query}")
    return bool(runs["workflow_runs"])


def synchronize(root: Path, repository: str, *, rerun_ci: bool = False) -> dict:
    if repository != DESTINATION:
        raise ValueError(f"This workflow only manages {DESTINATION}")
    api = GitHub(root)
    metadata = api.api(f"repos/{repository}")
    if metadata.get("parent", {}).get("full_name") != OFFICIAL:
        raise RuntimeError("Unexpected upstream parent")
    base_branch = metadata["default_branch"]
    git(root, "check-ref-format", "--branch", base_branch)
    git(root, "fetch", "--no-tags", "origin", f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}")
    git(root, "fetch", "--no-tags", f"https://github.com/{OFFICIAL}.git", "+refs/heads/main:refs/remotes/official/main")
    base = git(root, "rev-parse", f"refs/remotes/origin/{base_branch}").stdout.strip()
    upstream = git(root, "rev-parse", "refs/remotes/official/main").stdout.strip()
    result = {"repository": repository, "base_sha": base, "upstream_sha": upstream, "ci_dispatched": False}
    if ancestor(root, upstream, base):
        result["state"] = "up_to_date"
        if rerun_ci:
            dispatch_ci(api, repository, base_branch)
            result["ci_dispatched"] = True
        return result
    branch = candidate_name(base, upstream)
    result["branch"] = branch
    existing = git(root, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").stdout.strip()
    if existing:
        # Preserve any human resolution already pushed to this candidate.
        git(root, "fetch", "--no-tags", "origin", f"refs/heads/{branch}")
        head = git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
        result.update(state="ready" if ancestor(root, base, head) and ancestor(root, upstream, head) else "conflicts", conflicts=[], head_sha=head)
    else:
        result.update(merge_candidate(root, base, upstream, branch))
        git(root, "push", "origin", f"HEAD:refs/heads/{branch}")
    pr = open_pr(api, repository, base_branch, branch, result, upstream)
    result["pull_request"] = pr
    # Retry a missing dispatch after a previous API/network error. A completed
    # failing CI run is still a real result and is not silently retried daily.
    if result["state"] == "ready" and (rerun_ci or not has_dispatched_ci(api, repository, branch, result["head_sha"])):
        dispatch_ci(api, repository, branch)
        result["ci_dispatched"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--rerun-ci", action="store_true")
    args = parser.parse_args()
    try:
        result = synchronize(Path.cwd(), args.repository, rerun_ci=args.rerun_ci)
    except Exception as exc:
        result = {"state": "error", "error": str(exc)}
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.summary:
        args.summary.write_text("## Upstream sync\n\n```json\n" + json.dumps(result, indent=2, ensure_ascii=False) + "\n```\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["state"] in {"conflicts", "error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
