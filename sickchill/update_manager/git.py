import shutil
import subprocess

from sickchill import logger, settings
from sickchill.init_helpers import sickchill_dir
from sickchill.oldbeard import helpers, notifiers

from .abstract import UpdateManagerBase


class GitUpdateManager(UpdateManagerBase):
    def __init__(self):
        self._git_path = settings.GIT_PATH or shutil.which("git")
        self._current_commit_hash = None
        self._newest_commit_hash = None
        self._num_commits_behind = 0
        self._num_commits_ahead = 0
        self._current_branch = None

    @property
    def _target_ref(self):
        return f"refs/remotes/sickchill-updater/{settings.GIT_BRANCH}"

    def _run_git(self, *args, log_errors=True):
        if not self._git_path:
            logger.warning("Unable to find the git executable, can't check for updates")
            return None, 127

        cmd = [self._git_path, *args]
        logger.debug(f"Executing {' '.join(cmd)} in {sickchill_dir.parent}")
        try:
            process = subprocess.run(
                cmd,
                cwd=sickchill_dir.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        except OSError as error:
            logger.warning(f"Unable to run git: {error}")
            return None, 126

        output = process.stdout.strip()
        if process.returncode and log_errors:
            logger.warning(f"git {' '.join(args)} returned {process.returncode}: {output}")
        return output, process.returncode

    def _refresh(self):
        self._num_commits_behind = 0
        self._num_commits_ahead = 0

        _, exit_status = self._run_git("check-ref-format", f"refs/heads/{settings.GIT_BRANCH}")
        if exit_status:
            logger.warning(f"Invalid update branch: {settings.GIT_BRANCH}")
            return False

        refspec = f"+refs/heads/{settings.GIT_BRANCH}:{self._target_ref}"
        _, exit_status = self._run_git("fetch", "--no-tags", "--prune", settings.GIT_REMOTE_URL, refspec)
        if exit_status:
            logger.warning(f"Unable to fetch updates from {settings.GIT_REMOTE_URL}")
            return False

        self._current_branch, _ = self._run_git("symbolic-ref", "--quiet", "--short", "HEAD", log_errors=False)
        self._current_commit_hash, exit_status = self._run_git("rev-parse", "HEAD")
        if exit_status:
            return False

        self._newest_commit_hash, exit_status = self._run_git("rev-parse", self._target_ref)
        if exit_status:
            return False

        output, exit_status = self._run_git("rev-list", "--left-right", "--count", f"HEAD...{self._target_ref}")
        if exit_status or not output:
            return False

        try:
            ahead, behind = output.split()
            self._num_commits_ahead = int(ahead)
            self._num_commits_behind = int(behind)
        except (TypeError, ValueError):
            logger.warning(f"Unable to parse git ahead/behind counts: {output}")
            return False

        logger.debug(
            f"current_commit={self._current_commit_hash}, newest_commit={self._newest_commit_hash}, "
            f"commits_behind={self._num_commits_behind}, commits_ahead={self._num_commits_ahead}"
        )
        return True

    def get_current_version(self):
        return self._current_commit_hash

    def get_newest_version(self):
        return self._newest_commit_hash

    def get_version_delta(self):
        if not self._refresh():
            return 0
        return self._num_commits_behind

    def set_newest_text(self):
        if not self._num_commits_behind:
            return

        if self._current_branch != settings.GIT_BRANCH:
            newest_tag = "update_wrong_branch"
            newest_text = _("There are updates available, but automatic update is disabled while on branch {current}. " "Switch to {target} to update.").format(
                current=self._current_branch or "detached HEAD", target=settings.GIT_BRANCH
            )
            level = "warning"
        elif self._num_commits_ahead:
            newest_tag = "update_diverged"
            newest_text = _("The local branch has diverged from {target}. Automatic update is disabled; reconcile the branches manually.").format(
                target=settings.GIT_BRANCH
            )
            level = "warning"
        else:
            commits = self._num_commits_behind
            compare_url = f"https://github.com/{settings.GIT_ORG}/{settings.GIT_REPO}/compare/" f"{self._current_commit_hash}...{self._newest_commit_hash}"
            newest_tag = "newer_version_available"
            newest_text = _(
                'There is a <a href="{url}" target="_blank" rel="noreferrer">newer version available</a> '
                '({commits} commit{plural} behind) &mdash; <a href="{update_url}">Update Now</a>'
            ).format(
                url=compare_url,
                commits=commits,
                plural="" if commits == 1 else "s",
                update_url=self.get_update_url(),
            )
            level = "success"

        helpers.add_site_message(newest_text, tag=newest_tag, level=level)

    def need_update(self):
        return self._refresh() and self._num_commits_behind > 0

    def update(self):
        if not self.need_update():
            return False

        if self._current_branch != settings.GIT_BRANCH:
            logger.warning(f"Automatic update requires branch {settings.GIT_BRANCH}; current branch is {self._current_branch or 'detached HEAD'}")
            return False

        if self._num_commits_ahead:
            logger.warning("The local update branch has diverged from the remote; refusing to update automatically")
            return False

        output, exit_status = self._run_git("status", "--porcelain")
        if exit_status or output:
            logger.warning("The working tree has local changes; refusing to update automatically")
            return False

        _, exit_status = self._run_git("merge", "--ff-only", self._target_ref)
        if exit_status:
            return False

        self._current_commit_hash, _ = self._run_git("rev-parse", "HEAD")
        notifiers.notify_update(self._current_commit_hash or "")
        return True
