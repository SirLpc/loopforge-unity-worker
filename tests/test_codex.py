import unittest


class CodexOutputTests(unittest.TestCase):
    def test_review_verdict_and_issue_schema_are_enforced(self):
        from pydantic import ValidationError
        from codex_executor import ReviewResult

        for value in ('{"verdict":"ok","summary":"","issues":[]}',
                      '{"verdict":"pass","summary":"","issues":[{"severity":"high"}]}',
                      '{"verdict":"pass","summary":"","issues":[{"severity":"high","file":"a","line":1,"description":"b","suggestion":"c"}]}'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ReviewResult.model_validate_json(value)
        result = ReviewResult.model_validate_json('{"verdict":"pass","summary":"Checked","issues":[]}')
        self.assertEqual(result.verdict, "pass")

    def test_slot_permissions_deny_sibling_checkouts(self):
        from pathlib import Path
        from types import SimpleNamespace
        from codex_executor import permission_args, toml_string

        own = Path.cwd() / "slot-one"
        sibling = Path.cwd() / "slot-two"
        review = Path.cwd() / "review"
        settings = SimpleNamespace(project=own, codex_exe=str(Path.cwd() / "tools/codex.exe"),
                                   codex_home=Path.cwd() / "codex-home", codex_sandbox="mxc",
                                   sibling_checkouts=(sibling,))
        for cwd, read_only in ((own, False), (review, True)):
            with self.subTest(review=read_only):
                arguments = " ".join(permission_args(settings, cwd, review=read_only))
                self.assertIn(f'{toml_string(sibling)}="deny"', arguments)
                self.assertIn(f'{toml_string(cwd)}="{"read" if read_only else "write"}"', arguments)

    def test_root_project_protects_configured_caches_and_tracked_outputs(self):
        from pathlib import Path
        from types import SimpleNamespace
        from codex_executor import permission_args, toml_string

        own = Path.cwd() / "slot"
        settings = SimpleNamespace(project=own, codex_exe=str(own.parent / "codex.exe"),
                                   codex_home=own.parent / "codex-home", codex_sandbox="mxc",
                                   ignored_cache_paths=("Assets/Dependencies",), allowed_tracked_outputs=("Logs/Historic.log",))
        arguments = " ".join(permission_args(settings, own, review=False))
        for name in ("Assets/Dependencies", "Logs/Historic.log"):
            self.assertIn(f'{toml_string(own / name)}="deny"', arguments)
