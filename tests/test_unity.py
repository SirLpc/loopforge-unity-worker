import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import test_directory


def xml_result(failed=0, passed=1):
    outcome = "Failed" if failed else "Passed"
    cases = '<test-case result="Passed" />' * passed + '<test-case result="Failed" />' * failed
    return f'<test-run result="{outcome}" total="{failed + passed}" passed="{passed}" failed="{failed}" skipped="0" inconclusive="0">{cases}</test-run>'


class UnityEvidenceTests(unittest.TestCase):
    def test_compile_error_beats_zero_exit_code(self):
        from unity_executor import classify_editor_log

        self.assertEqual(classify_editor_log("Assets/Broken.cs(3,2): error CS1002: ; expected"), "compile_failed")
        self.assertEqual(classify_editor_log("Failed to resolve packages: Unable to add package"), "package_failed")
        self.assertEqual(classify_editor_log("No valid Unity Editor license found"), "license_failed")
        self.assertIsNone(classify_editor_log("[Licensing::Module] Successfully updated the license"))

    def test_xml_requires_executed_tests_and_blocks_failed_assertions(self):
        from process_runner import StageError
        from unity_executor import read_test_results

        with test_directory() as root:
            file = root / "results.xml"
            for contents, expected in [(None, "test_execution_failed"), ("<x />", "test_execution_failed"),
                                       (xml_result(0, 0), "not_run"), (xml_result(1, 0), "tests_failed")]:
                with self.subTest(expected):
                    if contents is not None:
                        file.write_text(contents, encoding="utf-8")
                    with self.assertRaises(StageError) as error:
                        read_test_results(file)
                    self.assertEqual(error.exception.kind, expected)
            file.write_text(xml_result(), encoding="utf-8")
            self.assertEqual(read_test_results(file)["passed"], 1)

    def test_xml_rejects_summary_without_matching_test_cases(self):
        from process_runner import StageError
        from unity_executor import read_test_results

        with test_directory() as root:
            result = root / "results.xml"
            result.write_text('<test-run result="Passed" total="1" passed="1" failed="0" />')
            with self.assertRaises(StageError):
                read_test_results(result)

    def test_missing_test_framework_is_unsupported_before_unity_start(self):
        from process_runner import StageError
        from unity_executor import UnityExecutor

        with test_directory() as project:
            unity = UnityExecutor(SimpleNamespace(project=project))
            with patch.object(unity, "validate"), self.assertRaises(StageError) as failure:
                unity.run(None, "editmode")
            self.assertEqual(failure.exception.kind, "unsupported")
