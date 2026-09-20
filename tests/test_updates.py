"""Where the in-place updater pulls from.

It pulled ``origin main`` whatever was checked out. On any other branch that
is not an update — it is a merge of main into your work, behind a hard reset
that has already thrown away anything uncommitted. A checkout records the
branch it tracks, and that is what these cover.
"""

import unittest
from unittest import mock

from support import SERVER_DIR, SplitflapTestCase  # noqa: F401  (sets up sys.path)

from splitflap import updates
from splitflap.updates import (
    commits_behind,
    describe,
    parse_github_repo,
    pull,
    update_target,
)


class FakeGit:
    """Answers the git questions the resolver asks, and records the rest."""

    def __init__(self, answers=None, fails=()):
        self.answers = dict(answers or {})
        self.fails = set(fails)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(list(args))
        key = " ".join(str(a) for a in args)
        if key in self.fails:
            return False, "fatal: no such thing"
        return True, self.answers.get(key, "")

    def ran(self, *prefix):
        return [call for call in self.calls if call[:len(prefix)] == list(prefix)]


ON_A_BRANCH = {
    "rev-parse --abbrev-ref HEAD": "refactor/modularize-server",
    "config --get branch.refactor/modularize-server.remote": "upstream",
    "config --get branch.refactor/modularize-server.merge":
        "refs/heads/refactor/modularize-server",
    "remote get-url upstream": "git@github.com:someone/splitflap-os.git",
}


class GithubUrlTests(unittest.TestCase):
    def test_every_form_git_writes_a_github_remote_in(self):
        for url in (
            "https://github.com/csader/splitflap-os.git",
            "https://github.com/csader/splitflap-os",
            "http://github.com/csader/splitflap-os/",
            "git@github.com:csader/splitflap-os.git",
            "ssh://git@github.com/csader/splitflap-os.git",
            "git://github.com/csader/splitflap-os.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(parse_github_repo(url), "csader/splitflap-os")

    def test_anything_that_is_not_github_has_no_repository(self):
        for url in ("https://gitlab.com/someone/thing.git", "/srv/mirror/thing.git",
                    "", None, "https://github.com/csader"):
            with self.subTest(url=url):
                self.assertIsNone(parse_github_repo(url))


class UpdateTargetTests(unittest.TestCase):
    def resolve(self, git):
        with mock.patch.object(updates, "git", git):
            return update_target()

    def test_the_branch_that_is_checked_out_is_the_one_that_is_tracked(self):
        git = FakeGit(ON_A_BRANCH)

        target = self.resolve(git)

        self.assertEqual(target["branch"], "refactor/modularize-server")
        self.assertEqual(target["remote"], "upstream")
        self.assertEqual(target["remote_branch"], "refactor/modularize-server")
        self.assertEqual(target["repo"], "someone/splitflap-os")
        self.assertEqual(describe(target), "upstream/refactor/modularize-server")

    def test_a_branch_pushed_under_another_name_keeps_that_name(self):
        # branch.<name>.merge is the only place this is written down.
        git = FakeGit({
            "rev-parse --abbrev-ref HEAD": "local-name",
            "config --get branch.local-name.remote": "origin",
            "config --get branch.local-name.merge": "refs/heads/their-name",
        })

        self.assertEqual(self.resolve(git)["remote_branch"], "their-name")

    def test_an_untracked_branch_falls_back_to_its_own_name_on_origin(self):
        git = FakeGit(
            {"rev-parse --abbrev-ref HEAD": "scratch"},
            fails=["config --get branch.scratch.remote",
                   "config --get branch.scratch.merge"],
        )

        target = self.resolve(git)

        self.assertEqual(describe(target), "origin/scratch")

    def test_a_detached_head_is_said_out_loud_rather_than_guessed_at(self):
        # Guessing main here is how a branch gets replaced by someone else's.
        git = FakeGit({"rev-parse --abbrev-ref HEAD": "HEAD"})

        target = self.resolve(git)

        self.assertTrue(target["detached"])
        self.assertIsNone(target["remote"])
        self.assertEqual(describe(target), "")

    def test_a_checkout_git_cannot_read_is_treated_as_detached(self):
        git = FakeGit(fails=["rev-parse --abbrev-ref HEAD"])

        self.assertTrue(self.resolve(git)["detached"])

    def test_a_remote_that_is_not_github_still_resolves_release_notes(self):
        git = FakeGit(dict(ON_A_BRANCH,
                           **{"remote get-url upstream": "/srv/mirror/splitflap.git"}))

        self.assertEqual(self.resolve(git)["repo"], updates.DEFAULT_REPO)


class CommitsBehindTests(unittest.TestCase):
    def setUp(self):
        self.target = {"branch": "topic", "remote": "origin",
                       "remote_branch": "topic", "detached": False}

    def count(self, git):
        with mock.patch.object(updates, "git", git):
            return commits_behind(self.target)

    def test_it_counts_what_the_tracked_branch_has_that_we_do_not(self):
        git = FakeGit({"rev-list --count HEAD..FETCH_HEAD": "4"})

        self.assertEqual(self.count(git), 4)
        self.assertEqual(git.ran("fetch"), [["fetch", "origin", "topic"]])

    def test_up_to_date_is_zero_and_not_unknown(self):
        self.assertEqual(self.count(FakeGit({"rev-list --count HEAD..FETCH_HEAD": "0"})), 0)

    def test_a_remote_it_cannot_reach_is_unknown_rather_than_up_to_date(self):
        git = FakeGit(fails=["fetch origin topic"])

        self.assertIsNone(self.count(git))

    def test_a_count_that_is_not_a_count_is_unknown(self):
        self.assertIsNone(self.count(FakeGit({"rev-list --count HEAD..FETCH_HEAD": ""})))

    def test_a_detached_head_is_never_fetched_for(self):
        self.target = {"detached": True, "remote": None}
        git = FakeGit()

        self.assertIsNone(self.count(git))
        self.assertEqual(git.calls, [])


class PullTests(unittest.TestCase):
    def setUp(self):
        # pull() also asks git to trust the directory, which edits the global
        # git config of whoever is running the suite.
        patch = mock.patch.object(updates.subprocess, "run")
        self.subprocess_run = patch.start()
        self.addCleanup(patch.stop)

    def pull(self, git, target=None):
        target = target or {"branch": "topic", "remote": "upstream",
                            "remote_branch": "their-name", "detached": False}
        with mock.patch.object(updates, "git", git):
            return pull(target)

    def test_it_pulls_the_branch_we_are_on_and_names_no_other(self):
        git = FakeGit()

        ok, _ = self.pull(git)

        self.assertTrue(ok)
        self.assertEqual(git.ran("pull"), [["pull", "upstream", "their-name"]])
        self.assertNotIn("main", [arg for call in git.calls for arg in call])

    def test_local_changes_are_cleared_before_the_pull_and_not_after(self):
        git = FakeGit()

        self.pull(git)

        commands = [call[0] for call in git.calls]
        self.assertLess(commands.index("reset"), commands.index("pull"))

    def test_a_failed_pull_reports_what_git_said(self):
        git = FakeGit(fails=["pull upstream their-name"])

        ok, message = self.pull(git)

        self.assertFalse(ok)
        self.assertIn("no such thing", message)

    def test_a_detached_head_is_refused_before_anything_is_reset(self):
        git = FakeGit()

        ok, message = self.pull(git, {"detached": True, "remote": None})

        self.assertFalse(ok)
        self.assertIn("not on a branch", message)
        self.assertEqual(git.calls, [])


class UpdaterSourceTests(unittest.TestCase):
    """Not by running it — applying an update restarts the service."""

    def test_no_branch_name_is_written_into_the_updater(self):
        source = (SERVER_DIR / "splitflap" / "web" / "system.py").read_text(encoding="utf-8")
        self.assertNotIn("'main'", source)
        self.assertNotIn('"main"', source)

    def test_the_route_pulls_through_the_resolver(self):
        source = (SERVER_DIR / "splitflap" / "web" / "system.py").read_text(encoding="utf-8")
        self.assertIn("pull(target)", source)
        self.assertIn("update_target()", source)


if __name__ == "__main__":
    unittest.main()
