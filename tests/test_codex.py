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
