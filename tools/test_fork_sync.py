"""Exercise real Git merges and the GitHub PR / CI handoff without networking."""
from pathlib import Path
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fork_sync as sync


@pytest.fixture
def history(tmp_path):
    sync.git(tmp_path, "init", "-b", "main")
    sync.git(tmp_path, "config", "user.name", "Test")
    sync.git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "shared.txt").write_text("initial\n", encoding="utf-8")
    sync.git(tmp_path, "add", ".")
    sync.git(tmp_path, "commit", "-m", "initial")
    initial = sync.git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    def commit(branch, filename, text):
        sync.git(tmp_path, "switch", "-C", branch, initial)
        (tmp_path / filename).write_text(text, encoding="utf-8")
        sync.git(tmp_path, "add", filename)
        sync.git(tmp_path, "commit", "-m", branch)
        return sync.git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    return tmp_path, commit


def test_clean_merge_keeps_both_local_and_official_changes(history):
    root, commit = history
    base = commit("main", "ours.txt", "our custom provider\n")
    official = commit("official", "upstream.txt", "upstream fix\n")
    result = sync.merge_candidate(root, base, official, sync.candidate_name(base, official))
    assert result["state"] == "ready"
    assert (root / "ours.txt").read_text() == "our custom provider\n"
    assert (root / "upstream.txt").read_text() == "upstream fix\n"
    assert sync.ancestor(root, base, result["head_sha"])
    assert sync.ancestor(root, official, result["head_sha"])
    assert sync.git(root, "rev-parse", "main").stdout.strip() == base


def test_conflict_reports_file_and_never_changes_main(history):
    root, commit = history
    base = commit("main", "shared.txt", "our customization\n")
    official = commit("official", "shared.txt", "official edit\n")
    result = sync.merge_candidate(root, base, official, sync.candidate_name(base, official))
    assert result["state"] == "conflicts"
    assert result["conflicts"] == ["shared.txt"]
    assert result["head_sha"] == official
    assert sync.git(root, "show", "main:shared.txt").stdout == "our customization\n"
    assert sync.git(root, "rev-parse", "main").stdout.strip() == base
    assert not sync.git(root, "status", "--porcelain").stdout.strip()


@pytest.mark.parametrize("branch", ["main", "automation/upstream-../../main"])
def test_rejects_unsafe_branch(history, branch):
    root, _ = history
    head = sync.git(root, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises((ValueError, RuntimeError)):
        sync.merge_candidate(root, head, head, branch)
    assert sync.git(root, "branch", "--show-current").stdout.strip() == "main"


def test_dirty_checkout_is_preserved(history):
    root, _ = history
    head = sync.git(root, "rev-parse", "HEAD").stdout.strip()
    (root / "shared.txt").write_text("unsaved user edit\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="dirty"):
        sync.merge_candidate(root, head, head, sync.candidate_name(head, head))
    assert (root / "shared.txt").read_text() == "unsaved user edit\n"


class FakeAPI:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    def api(self, path, *, method="GET", body=None):
        self.calls.append((path, method, body))
        return next(self.answers)


def test_conflict_opens_draft_pr_and_dispatch_uses_candidate_ref():
    api = FakeAPI([[], {"html_url": "https://example.invalid/pr/1", "number": 1}, None])
    result = {"state": "conflicts", "conflicts": ["shared.txt"]}
    branch = "automation/upstream-test"
    pr = sync.open_pr(api, sync.DESTINATION, "main", branch, result, "a" * 40)
    assert pr["created"]
    payload = api.calls[1][2]
    assert payload["draft"] is True
    assert payload["base"] == "main"
    assert payload["head"] == branch
    assert "shared.txt" in payload["body"]
    sync.dispatch_ci(api, sync.DESTINATION, branch)
    assert api.calls[2][0].endswith("/fork-ci.yml/dispatches")
    assert api.calls[2][1:] == ("POST", {"ref": branch})


def test_reuses_existing_pr_without_overwriting_it():
    api = FakeAPI([[{"html_url": "https://example.invalid/pr/1", "number": 1}]])
    pr = sync.open_pr(api, sync.DESTINATION, "main", "automation/upstream-test", {}, "a" * 40)
    assert pr["created"] is False
    assert len(api.calls) == 1


def test_disabled_bot_pr_creation_keeps_candidate_available():
    class RestrictedAPI(FakeAPI):
        def api(self, path, *, method="GET", body=None):
            if method == "POST":
                raise RuntimeError("GitHub Actions is not permitted to create or approve pull requests. (HTTP 403)")
            return []
    api = RestrictedAPI([])
    pr = sync.open_pr(api, sync.DESTINATION, "main", "automation/upstream-test", {"state": "ready", "conflicts": []}, "a" * 40)
    assert pr["manual_creation_required"] is True
    assert "/compare/main...automation/upstream-test" in pr["url"]


def test_ci_lookup_distinguishes_missing_dispatch_and_failed_completed_run():
    api = FakeAPI([{"workflow_runs": []}, {"workflow_runs": [{"conclusion": "failure"}]}])
    assert not sync.has_dispatched_ci(api, sync.DESTINATION, "automation/upstream-test", "b" * 40)
    assert sync.has_dispatched_ci(api, sync.DESTINATION, "automation/upstream-test", "b" * 40)
    assert "event=workflow_dispatch" in api.calls[0][0]
    assert "head_sha=" + "b" * 40 in api.calls[0][0]


@pytest.mark.parametrize("conflict", [False, True])
def test_full_sync_with_real_remote_keeps_main_and_dispatches_only_merged_tree(history, tmp_path_factory, monkeypatch, conflict):
    root, commit = history
    base = commit("main", "shared.txt" if conflict else "ours.txt", "our customization\n")
    official = commit("official", "shared.txt" if conflict else "upstream.txt", "official edit\n")
    remote = tmp_path_factory.mktemp("bare-origin")
    sync.git(root, "clone", "--bare", str(root), str(remote))
    sync.git(root, "remote", "add", "origin", str(remote))
    real_git = sync.git

    def local_only_git(root, *args, **kwargs):
        if args[:3] == ("fetch", "--no-tags", f"https://github.com/{sync.OFFICIAL}.git"):
            args = ("fetch", "--no-tags", "origin", "+refs/heads/official:refs/remotes/official/main")
        return real_git(root, *args, **kwargs)

    class RepositoryAPI:
        def __init__(self):
            self.dispatches = []

        def api(self, path, *, method="GET", body=None):
            if path == f"repos/{sync.DESTINATION}":
                return {"parent": {"full_name": sync.OFFICIAL}, "default_branch": "main"}
            if method == "POST" and path.endswith("/pulls"):
                raise RuntimeError("GitHub Actions is not permitted to create or approve pull requests. (HTTP 403)")
            if "/pulls?" in path:
                return []
            if "/runs?" in path:
                return {"workflow_runs": []}
            if path.endswith("/dispatches"):
                self.dispatches.append(body)
                return None
            raise AssertionError((path, method, body))

    api = RepositoryAPI()
    monkeypatch.setattr(sync, "GitHub", lambda _: api)
    monkeypatch.setattr(sync, "git", local_only_git)
    result = sync.synchronize(root, sync.DESTINATION)
    assert result["state"] == ("conflicts" if conflict else "ready")
    assert result["pull_request"]["manual_creation_required"] is True
    assert result["ci_dispatched"] is (not conflict)
    assert api.dispatches == ([] if conflict else [{"ref": result["branch"]}])
    assert real_git(root, "--git-dir", str(remote), "rev-parse", "main").stdout.strip() == base
    candidate = real_git(root, "--git-dir", str(remote), "rev-parse", result["branch"]).stdout.strip()
    assert candidate == result["head_sha"]
    assert sync.ancestor(root, official, candidate)
    if not conflict:
        assert sync.ancestor(root, base, candidate)
        assert (root / "ours.txt").read_text() == "our customization\n"


def test_workflows_have_daily_and_manual_triggers_and_explicit_write_scopes():
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    # BaseLoader avoids YAML 1.1 interpreting the Actions `on` key as a boolean.
    updater = yaml.load((workflows / "sync-upstream.yml").read_text(), Loader=yaml.BaseLoader)
    ci = yaml.load((workflows / "fork-ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert updater["on"]["schedule"][0]["cron"] == "17 2 * * *"
    assert "workflow_dispatch" in updater["on"]
    assert updater["permissions"] == {"contents": "read"}
    assert updater["jobs"]["sync"]["permissions"] == {"contents": "write", "pull-requests": "write", "actions": "write"}
    assert "workflow_dispatch" in ci["on"]
    assert ci["on"]["pull_request"]["branches"] == ["main"]
    assert ci["permissions"] == {"contents": "read"}
    assert {job["runs-on"] for job in ci["jobs"].values()} == {"ubuntu-latest", "windows-latest"}
