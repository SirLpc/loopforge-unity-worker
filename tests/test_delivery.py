import unittest
from types import SimpleNamespace


class PullRequestsTests(unittest.TestCase):
    def test_removed_adapter_is_a_recoverable_pr_error(self):
        from development import DevelopmentWorker
        from process_runner import StageError

        ctx = SimpleNamespace(check_cancelled=lambda: None, task={})
        with self.assertRaises(StageError) as error:
            DevelopmentWorker.publish_pr(SimpleNamespace(pr_adapter=None), ctx)
        self.assertEqual(error.exception.kind, "pr_failed")

    def test_gitlab_encodes_project_and_reuses_existing_mr(self):
        from pull_requests import HostedPullRequests

        adapter = HostedPullRequests("gitlab", "https://git.example/api/v4", "team/game", "fixture-token")
        calls = []

        def request(path, payload=None):
            calls.append((path, payload))
            return [{"iid": 17, "web_url": "https://git.example/team/game/-/merge_requests/17", "state": "opened"}]

        adapter.request = request
        result = adapter.create({"task_branch": "ai/task-abc", "base_branch": "main", "task_id": "abc"})
        self.assertEqual(result["id"], 17)
        self.assertIn("/projects/team%2Fgame/merge_requests?", calls[0][0])
        self.assertIn("source_branch=ai%2Ftask-abc", calls[0][0])
        self.assertEqual(len(calls), 1)

    def test_github_creates_pull_request_with_fixed_head_and_base(self):
        from pull_requests import HostedPullRequests

        adapter = HostedPullRequests("github", "https://api.github.com", "team/game", "fixture-token")
        calls = []

        def request(path, payload=None):
            calls.append((path, payload))
            return {"number": 3, "html_url": "https://github.com/team/game/pull/3", "state": "open"} if payload else []

        adapter.request = request
        result = adapter.create({"task_branch": "ai/task-abc", "base_branch": "integration", "task_id": "abc"})
        self.assertEqual(result["url"], "https://github.com/team/game/pull/3")
        self.assertEqual(calls[1][1]["head"], "ai/task-abc")
        self.assertEqual(calls[1][1]["base"], "integration")
        self.assertEqual(calls[1][0], "/repos/team/game/pulls")
