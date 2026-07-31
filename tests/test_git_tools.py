"""
Tests for the git operations tools (git_status/git_diff/git_log/git_commit).
These run against REAL git repositories in a tmp_path — no external
dependency, so unlike GitHub/Jira/Linear/Graph/Salesforce this is fully
testable end-to-end rather than mocked at the HTTP layer.
"""
import asyncio
import subprocess

import pytest

from app import db
from app.models.connector import AuthorizedDirectory
from app.services.mcp import tools as mcp_tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
                        "PATH": "/usr/bin:/bin"})


@pytest.fixture
def git_repo(app, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=str(repo))
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=str(repo))
    _git("commit", "-q", "-m", "initial commit", cwd=str(repo))
    with app.app_context():
        d = AuthorizedDirectory(id="d-git1", path=str(repo), alias="git-repo", enabled=True)
        db.session.add(d)
        db.session.commit()
        yield repo
        AuthorizedDirectory.query.filter_by(id="d-git1").delete()
        db.session.commit()


class TestAuthorizeGitRepo:
    def test_rejects_unauthorized_path(self, app, tmp_path):
        other = tmp_path / "not-authorized"
        other.mkdir()
        _git("init", "-q", cwd=str(other))
        with app.app_context():
            result = _run(mcp_tools._handle_git_status("git_status", {"path": str(other)}))
        assert "not authorized" in result

    def test_rejects_authorized_dir_that_is_not_a_git_repo(self, app, tmp_path):
        not_a_repo = tmp_path / "plain-dir"
        not_a_repo.mkdir()
        with app.app_context():
            d = AuthorizedDirectory(id="d-notgit", path=str(not_a_repo), alias="x", enabled=True)
            db.session.add(d)
            db.session.commit()
            try:
                result = _run(mcp_tools._handle_git_status("git_status", {"path": str(not_a_repo)}))
                assert "not a git repository" in result
            finally:
                AuthorizedDirectory.query.filter_by(id="d-notgit").delete()
                db.session.commit()


class TestGitStatus:
    def test_clean_repo(self, app, git_repo):
        with app.app_context():
            result = _run(mcp_tools._handle_git_status("git_status", {"path": str(git_repo)}))
        assert "clean working tree" in result

    def test_shows_untracked_file(self, app, git_repo):
        (git_repo / "new.txt").write_text("new content\n")
        with app.app_context():
            result = _run(mcp_tools._handle_git_status("git_status", {"path": str(git_repo)}))
        assert "new.txt" in result


class TestGitDiff:
    def test_no_diff_on_clean_repo(self, app, git_repo):
        with app.app_context():
            result = _run(mcp_tools._handle_git_diff("git_diff", {"path": str(git_repo)}))
        assert "no differences" in result

    def test_shows_unstaged_changes(self, app, git_repo):
        (git_repo / "README.md").write_text("hello\nmodified\n")
        with app.app_context():
            result = _run(mcp_tools._handle_git_diff("git_diff", {"path": str(git_repo)}))
        assert "modified" in result
        assert "README.md" in result

    def test_rejects_flag_like_ref(self, app, git_repo):
        with app.app_context():
            result = _run(mcp_tools._handle_git_diff("git_diff", {
                "path": str(git_repo), "ref": "--upload-pack=evil",
            }))
        assert "must not start with '-'" in result


class TestGitLog:
    def test_shows_initial_commit(self, app, git_repo):
        with app.app_context():
            result = _run(mcp_tools._handle_git_log("git_log", {"path": str(git_repo)}))
        assert "initial commit" in result

    def test_respects_limit(self, app, git_repo):
        for i in range(5):
            (git_repo / f"f{i}.txt").write_text(str(i))
            _git("add", f"f{i}.txt", cwd=str(git_repo))
            _git("commit", "-q", "-m", f"commit {i}", cwd=str(git_repo))
        with app.app_context():
            result = _run(mcp_tools._handle_git_log("git_log", {"path": str(git_repo), "limit": 2}))
        lines = [l for l in result.strip().split("\n") if l]
        assert len(lines) == 2
        assert "commit 4" in result
        assert "commit 0" not in result


class TestGitCommit:
    def test_stages_and_commits_explicit_paths(self, app, git_repo):
        (git_repo / "a.txt").write_text("a")
        (git_repo / "b.txt").write_text("b")  # not passed — must not be committed
        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "add a.txt", "paths": ["a.txt"],
            }))
        assert not result.startswith("Error"), result

        log = subprocess.run(["git", "log", "--stat", "-1"], cwd=str(git_repo),
                             capture_output=True, text=True).stdout
        assert "a.txt" in log
        assert "b.txt" not in log
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo),
                                capture_output=True, text=True).stdout
        assert "b.txt" in status  # b.txt is still untracked, never staged

    def test_commit_succeeds_with_no_ambient_git_identity(self, app, git_repo):
        # _run_git deliberately blocks system/global git config (RCE
        # hardening) — that also strips any user.name/user.email an
        # operator configured globally, which used to make every commit
        # fail with "unable to auto-detect email address". git_commit must
        # supply its own identity explicitly rather than depend on that.
        (git_repo / "c.txt").write_text("c")
        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "add c.txt", "paths": ["c.txt"],
            }))
        assert not result.startswith("Error"), result
        author = subprocess.run(["git", "log", "-1", "--pretty=format:%an <%ae>"],
                                cwd=str(git_repo), capture_output=True, text=True).stdout
        assert author == "Orion's Belt Agent <agent@orions-belt.local>"

    def test_requires_message(self, app, git_repo):
        (git_repo / "a.txt").write_text("a")
        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "paths": ["a.txt"],
            }))
        assert "message is required" in result

    def test_requires_nonempty_paths(self, app, git_repo):
        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "msg", "paths": [],
            }))
        assert "paths is required" in result

    def test_rejects_path_outside_the_repo(self, app, git_repo, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "msg", "paths": [str(outside)],
            }))
        assert "not authorized" in result or "outside the target repo" in result

    def test_rejects_dot_as_a_path_add_all_loophole(self, app, git_repo):
        """paths: ["."] resolves to the repo root itself — staging it is
        equivalent to `git add -A`, exactly what taking explicit `paths`
        instead of an "add everything" flag is meant to prevent (an agent
        could stage and commit a .env or other file it never meant to)."""
        (git_repo / "a.txt").write_text("a")
        (git_repo / "secret.env").write_text("SECRET=1")
        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "msg", "paths": ["."],
            }))
        assert result.startswith("Error")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo),
                                capture_output=True, text=True).stdout
        assert "a.txt" in status  # still untracked — nothing was staged/committed

    def test_rejects_a_directory_path(self, app, git_repo):
        """Only explicit files are accepted — a subdirectory would stage
        everything under it, the same "add everything" loophole as "."."""
        (git_repo / "subdir").mkdir()
        (git_repo / "subdir" / "a.txt").write_text("a")
        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "msg", "paths": ["subdir"],
            }))
        assert result.startswith("Error")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo),
                                capture_output=True, text=True).stdout
        assert "subdir/a.txt" in status or "subdir/" in status  # still untracked


class TestGitConfigHardening:
    def test_malicious_diff_textconv_is_not_executed(self, app, git_repo):
        """A repo's own .git/config can point diff.external or a *.textconv
        filter at an arbitrary command — those apply even to a read-only
        `git diff`. This is the exact bypass that would let Tier-1
        create_file (writing .git/config) + Tier-0 git_diff execute
        commands, routing around the Tier-3 gate run_shell exists to
        enforce. Verifies --no-textconv actually neutralizes it."""
        marker = git_repo / "PWNED"
        (git_repo / ".git" / "config").write_text(
            (git_repo / ".git" / "config").read_text() +
            f"\n[diff \"evil\"]\n\ttextconv = touch {marker}\n"
        )
        (git_repo / ".gitattributes").write_text("README.md diff=evil\n")
        _git("add", ".gitattributes", cwd=str(git_repo))
        _git("commit", "-q", "-m", "add attributes", cwd=str(git_repo))
        (git_repo / "README.md").write_text("hello\nchanged\n")

        with app.app_context():
            _run(mcp_tools._handle_git_diff("git_diff", {"path": str(git_repo)}))

        assert not marker.exists(), "textconv command executed — .git/config RCE bypass not neutralized"

    def test_core_fsmonitor_is_not_executed(self, app, git_repo):
        marker = git_repo / "FSMONITOR_RAN"
        (git_repo / ".git" / "config").write_text(
            (git_repo / ".git" / "config").read_text() +
            f'\n[core]\n\tfsmonitor = "touch {marker}"\n'
        )
        with app.app_context():
            _run(mcp_tools._handle_git_status("git_status", {"path": str(git_repo)}))
        assert not marker.exists(), "core.fsmonitor command executed — .git/config RCE bypass not neutralized"

    def test_malicious_filter_clean_is_refused_not_executed(self, app, git_repo):
        """filter.<name>.clean/smudge/process is a content filter driver —
        the driver name (here "evil") is entirely attacker-chosen, so unlike
        core.fsmonitor/core.pager there is no FIXED key _run_git could
        override to neutralize it. This must be detected and the whole
        operation refused, not selectively patched — verified against a
        real repo where the naive per-key -c overrides alone (the original
        implementation) let this run via a Tier-0 git_diff and a Tier-2
        git_commit (git add)."""
        marker = git_repo / "FILTER_RAN"
        (git_repo / ".git" / "config").write_text(
            (git_repo / ".git" / "config").read_text() +
            f'\n[filter "evil"]\n\tclean = touch {marker}\n\tsmudge = cat\n'
        )
        (git_repo / ".gitattributes").write_text("* filter=evil\n")

        with app.app_context():
            result_diff = _run(mcp_tools._handle_git_diff("git_diff", {"path": str(git_repo)}))
            result_status = _run(mcp_tools._handle_git_status("git_status", {"path": str(git_repo)}))

        assert not marker.exists(), "filter.clean command executed — RCE bypass not neutralized"
        assert "unsafe" in result_diff.lower() or "Error" in result_diff
        assert "unsafe" in result_status.lower() or "Error" in result_status

    def test_malicious_filter_clean_is_refused_on_commit(self, app, git_repo):
        marker = git_repo / "FILTER_RAN_COMMIT"
        (git_repo / ".git" / "config").write_text(
            (git_repo / ".git" / "config").read_text() +
            f'\n[filter "evil"]\n\tclean = touch {marker}\n\tsmudge = cat\n'
        )
        (git_repo / ".gitattributes").write_text("* filter=evil\n")
        (git_repo / "new.txt").write_text("content")

        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "msg", "paths": ["new.txt"],
            }))

        assert not marker.exists(), "filter.clean command executed via git add — RCE bypass not neutralized"
        assert result.startswith("Error")

    def test_malicious_gpg_program_is_refused_not_executed(self, app, git_repo):
        """gpg.program (combined with commit.gpgsign=true) is another
        attacker-chosen executable path with no fixed key to override."""
        marker = git_repo / "GPG_RAN"
        (git_repo / ".git" / "config").write_text(
            (git_repo / ".git" / "config").read_text() +
            f'\n[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /bin/sh -c "touch {marker}"\n'
        )
        (git_repo / "new.txt").write_text("content")

        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "msg", "paths": ["new.txt"],
            }))

        assert not marker.exists(), "gpg.program executed — RCE bypass not neutralized"
        assert result.startswith("Error")

    def test_safe_repo_config_is_unaffected(self, app, git_repo):
        """The safety check must not false-positive on an ordinary repo —
        every existing test in this file already exercises this implicitly,
        but assert it explicitly against a repo with a harmless custom
        section too."""
        (git_repo / ".git" / "config").write_text(
            (git_repo / ".git" / "config").read_text() +
            '\n[custom]\n\tsomeharmlesskey = somevalue\n'
        )
        with app.app_context():
            result = _run(mcp_tools._handle_git_status("git_status", {"path": str(git_repo)}))
        assert "clean working tree" in result

    def test_commit_gpgsign_alone_is_not_flagged(self, app, git_repo):
        """commit.gpgsign=true on its own can't execute anything (only
        gpg.program, checked separately, can) — it's an entirely ordinary
        setting on any repo where the operator signs commits, and it's
        already neutralized by the -c commit.gpgSign=false override
        regardless. Flagging it as 'dangerous' would refuse read-only
        git_status/git_diff on totally normal repos for no security benefit."""
        (git_repo / ".git" / "config").write_text(
            (git_repo / ".git" / "config").read_text() +
            '\n[commit]\n\tgpgsign = true\n'
        )
        with app.app_context():
            result = _run(mcp_tools._handle_git_status("git_status", {"path": str(git_repo)}))
        assert "clean working tree" in result

    def test_malicious_filter_via_include_path_is_refused_not_executed(self, app, git_repo):
        """`git config --local --list` does not reliably expand
        include.path/includeIf — a dangerous filter driver defined only in
        an INCLUDED file was invisible to a --local-scoped listing while
        still being fully effective. The safety check must read the same
        unscoped, include-expanded config the real git command will."""
        marker = git_repo / "INCLUDE_FILTER_RAN"
        (git_repo / ".git" / "evil.inc").write_text(
            f'[filter "evil"]\n\tclean = touch {marker}\n\tsmudge = cat\n'
        )
        (git_repo / ".git" / "config").write_text(
            (git_repo / ".git" / "config").read_text() +
            '\n[include]\n\tpath = evil.inc\n'
        )
        (git_repo / ".gitattributes").write_text("* filter=evil\n")

        with app.app_context():
            result_status = _run(mcp_tools._handle_git_status("git_status", {"path": str(git_repo)}))
            result_diff = _run(mcp_tools._handle_git_diff("git_diff", {"path": str(git_repo)}))

        assert not marker.exists(), "filter.clean reached via include.path executed — bypass not neutralized"
        assert "unsafe" in result_status.lower() or "Error" in result_status
        assert "unsafe" in result_diff.lower() or "Error" in result_diff

    def test_malicious_filter_via_worktree_config_is_refused_not_executed(self, app, git_repo):
        """`git config --local --list` excludes the worktree config scope
        (.git/config.worktree, active whenever extensions.worktreeConfig is
        set) entirely — a dangerous filter driver defined only there was
        invisible to a --local-scoped listing while still being fully
        effective. The safety check must read the same unscoped config the
        real git command will, which includes the worktree scope."""
        marker = git_repo / "WORKTREE_FILTER_RAN"
        (git_repo / ".git" / "config").write_text(
            (git_repo / ".git" / "config").read_text() +
            '\n[extensions]\n\tworktreeConfig = true\n'
        )
        (git_repo / ".git" / "config.worktree").write_text(
            f'[filter "evil"]\n\tclean = touch {marker}\n\tsmudge = cat\n'
        )
        (git_repo / ".gitattributes").write_text("* filter=evil\n")

        with app.app_context():
            result_status = _run(mcp_tools._handle_git_status("git_status", {"path": str(git_repo)}))
            result_diff = _run(mcp_tools._handle_git_diff("git_diff", {"path": str(git_repo)}))

        assert not marker.exists(), "filter.clean reached via worktree config executed — bypass not neutralized"
        assert "unsafe" in result_status.lower() or "Error" in result_status
        assert "unsafe" in result_diff.lower() or "Error" in result_diff


class TestGitCommitPathspecMagic:
    """git_commit's containment/isdir checks operate on the RESOLVED path of
    each input string — but a pathspec-magic string (a glob, or a `:`-prefixed
    long/short magic form) never resolves to an existing file or directory in
    the first place, so those checks silently don't apply to it, and it still
    reaches `git add -- <pathspec>` where git itself expands the magic. This
    is exactly the "add everything" behavior that taking explicit `paths`
    instead of a flag was meant to prevent, reachable through a different
    door than the "." case already covered above.
    --literal-pathspecs (set on every git invocation in _run_git) disables
    all pathspec magic globally, so these should now fail closed."""

    def test_rejects_glob_star_pathspec(self, app, git_repo):
        (git_repo / "a.txt").write_text("a")
        (git_repo / "secret.env").write_text("SECRET=1")
        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "msg", "paths": ["*"],
            }))
        assert result.startswith("Error")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo),
                                capture_output=True, text=True).stdout
        assert "secret.env" in status  # still untracked — nothing was staged/committed

    def test_rejects_long_magic_glob_pathspec(self, app, git_repo):
        (git_repo / "a.txt").write_text("a")
        (git_repo / "secret.env").write_text("SECRET=1")
        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "msg", "paths": [":(glob)**"],
            }))
        assert result.startswith("Error")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo),
                                capture_output=True, text=True).stdout
        assert "secret.env" in status

    def test_rejects_short_magic_top_pathspec(self, app, git_repo):
        (git_repo / "a.txt").write_text("a")
        (git_repo / "secret.env").write_text("SECRET=1")
        with app.app_context():
            result = _run(mcp_tools._handle_git_commit("git_commit", {
                "path": str(git_repo), "message": "msg", "paths": [":/"],
            }))
        assert result.startswith("Error")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo),
                                capture_output=True, text=True).stdout
        assert "secret.env" in status
