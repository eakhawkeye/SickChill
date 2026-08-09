from unittest import mock

import pytest

from sickchill import settings
from sickchill.update_manager.git import GitUpdateManager
from sickchill.update_manager.runner import UpdateManager


@pytest.fixture()
def updater(monkeypatch):
    monkeypatch.setattr(settings, "GIT_BRANCH", "master")
    monkeypatch.setattr(settings, "GIT_REMOTE_URL", "https://github.com/eakhawkeye/SickChill.git")
    fixture = GitUpdateManager()
    fixture._git_path = "git"
    return fixture


def git_result(ahead="0", behind="3", branch="master", dirty=""):
    def run_git(*args, **kwargs):
        if args[0] in ("check-ref-format", "fetch"):
            return "", 0
        if args[0] == "symbolic-ref":
            return branch, 0
        if args[:2] == ("rev-parse", "HEAD"):
            return "a" * 40, 0
        if args[0] == "rev-parse":
            return "b" * 40, 0
        if args[0] == "rev-list":
            return f"{ahead}\t{behind}", 0
        if args[0] == "status":
            return dirty, 0
        if args[0] == "merge":
            return "", 0
        raise AssertionError(f"Unexpected git call: {args}")

    return mock.Mock(side_effect=run_git)


class TestGitUpdateManager:
    def test_runner_selects_git_updater_for_checkout(self):
        assert isinstance(UpdateManager().updater, GitUpdateManager)

    def test_runner_executes_backup(self):
        update_manager = UpdateManager()
        update_manager._run_backup = mock.Mock(return_value=True)

        assert update_manager.backup()
        update_manager._run_backup.assert_called_once_with()

    def test_need_update_counts_remote_commits(self, updater):
        updater._run_git = git_result(behind="4")

        assert updater.need_update()
        assert updater.get_current_version() == "a" * 40
        assert updater.get_newest_version() == "b" * 40
        assert updater._num_commits_behind == 4
        assert updater._num_commits_ahead == 0

    def test_need_update_uses_configured_repository(self, updater):
        updater._run_git = git_result()

        assert updater.need_update()
        updater._run_git.assert_any_call(
            "fetch",
            "--no-tags",
            "--prune",
            "https://github.com/eakhawkeye/SickChill.git",
            "+refs/heads/master:refs/remotes/sickchill-updater/master",
        )

    def test_update_fast_forwards_clean_target_branch(self, updater, monkeypatch):
        updater._run_git = git_result()
        notify_update = mock.Mock()
        monkeypatch.setattr("sickchill.update_manager.git.notifiers.notify_update", notify_update)

        assert updater.update()
        updater._run_git.assert_any_call("merge", "--ff-only", "refs/remotes/sickchill-updater/master")
        notify_update.assert_called_once_with("a" * 40)

    @pytest.mark.parametrize(
        ("ahead", "branch", "dirty"),
        [("1", "master", ""), ("0", "feature", ""), ("0", "master", " M sickchill/settings.py")],
    )
    def test_update_refuses_unsafe_worktree(self, updater, ahead, branch, dirty):
        updater._run_git = git_result(ahead=ahead, branch=branch, dirty=dirty)

        assert not updater.update()
        assert not any(call.args[0] == "merge" for call in updater._run_git.call_args_list)
