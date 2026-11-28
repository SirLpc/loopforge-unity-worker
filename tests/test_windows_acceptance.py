import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import test_directory
from test_workspace import git
from windows_unity_acceptance import direct_merge_acceptance, fixture_transport


class FixtureReady(Exception):
    """Stop before starting the Worker after checking the real Git fixture setup."""


class MergeAcceptanceRemoteTests(unittest.TestCase):
    def setup_traffic(self, root, rewrite=None):
        project, remote, other = root / "project", root / "origin.git", root / "other.git"
        project.mkdir()
        (project / "tracked.txt").write_text("fixture")
        git(root, "init", "--bare", "--initial-branch=main", str(other))
        config = root / "global.gitconfig"
        if rewrite:
            key = {"fetch": f"url.{other.as_posix()}.insteadOf",
                   "push": f"url.{other.as_posix()}.pushInsteadOf",
                   "pushurl": "remote.origin.pushurl", "multiple": "remote.origin.pushurl"}[rewrite]
            git(root, "config", "--file", str(config), key,
                other.as_posix() if rewrite == "pushurl" else remote.as_posix())
            if rewrite == "multiple":
                git(root, "config", "--file", str(config), "--add", key, other.as_posix())
        settings = SimpleNamespace(project=project, unity_exe="unused", unity_version="2022.3.62f3", unity_timeout=1)
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}), \
                patch("worker.create_app", side_effect=FixtureReady):
            direct_merge_acceptance(root, settings)

    def test_redirected_urls_abort_before_the_initial_push(self):
        for rewrite in ("fetch", "push", "pushurl", "multiple"):
            with self.subTest(rewrite=rewrite), test_directory() as root:
                with self.assertRaisesRegex(AssertionError, "local bare"):
                    self.setup_traffic(root, rewrite)
                for remote in ("origin.git", "other.git"):
                    self.assertEqual(git(root / remote, "for-each-ref", "--format=%(refname)"), "")

    def test_normal_setup_pushes_only_to_the_generated_bare_remote(self):
        with test_directory() as root:
            with self.assertRaises(FixtureReady):
                self.setup_traffic(root)
            expected = git(root / "project", "rev-parse", "HEAD")
            self.assertEqual(git(root / "origin.git", "rev-parse", "main"), expected)
            remote, author = root / "origin.git", root / "target-author"
            fixture_transport(author, remote, "pull", "--ff-only", "origin", "main")
            self.assertEqual(git(author, "rev-parse", "HEAD"), expected)
            (author / "tracked.txt").write_text("independent target edit")
            git(author, "commit", "-am", "advance fixture target")
            fixture_transport(author, remote, "push", "origin", "main")
            self.assertEqual(git(remote, "rev-parse", "main"), git(author, "rev-parse", "HEAD"))
            # A later checkout-specific override must also be rejected before transport.
            git(author, "remote", "set-url", "--push", "origin", (root / "other.git").as_posix())
            with self.assertRaisesRegex(AssertionError, "local bare"):
                fixture_transport(author, remote, "push", "origin", "main")
            self.assertEqual(git(root / "other.git", "for-each-ref", "--format=%(refname)"), "")
