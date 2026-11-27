import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from support import test_directory


def xml_result(failed=0, passed=1):
    outcome = "Failed" if failed else "Passed"
    cases = '<test-case result="Passed" />' * passed + '<test-case result="Failed" />' * failed
    return f'<test-run result="{outcome}" total="{failed + passed}" passed="{passed}" failed="{failed}" skipped="0" inconclusive="0">{cases}</test-run>'


class UnityEvidenceTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Unity lock semantics")
    def test_batch_compile_cleans_only_its_unheld_empty_lock(self):
        from process_runner import StageError
        from unity_executor import UnityExecutor

        cases = (
            ("stale", b"", False, False),
            ("held", b"", True, True),
            ("nonempty", b"unexpected metadata", False, True),
        )
        for name, contents, held, remains in cases:
            with self.subTest(name=name), test_directory() as project, ExitStack() as handles:
                lock = project / "Temp/UnityLockfile"
                lock.parent.mkdir()
                log = project / "compile.log"
                cfg = SimpleNamespace(project=project, unity_exe="Unity.exe", unity_version="2022.3.62f3",
                                      unity_timeout=5)

                def run(argv, mode, **kwargs):
                    log.write_text("Assets/Test.cs(24,32): error CS0712: Cannot create a static class\n")
                    lock.write_bytes(contents)
                    if held:
                        handles.enter_context(lock.open("rb"))
                    return SimpleNamespace(exit_code=1)

                ctx = SimpleNamespace(artifact=lambda *args: log, run=run)
                with patch.object(UnityExecutor, "validate"), self.assertRaises(StageError) as failure:
                    UnityExecutor(cfg).run(ctx, "compile")
                self.assertEqual(failure.exception.kind, "compile_failed")
                self.assertEqual(failure.exception.result["log"], log.name)
                self.assertEqual(lock.exists(), remains)

    def test_scoped_run_uses_literal_filters_and_checks_each_fixture(self):
        from process_runner import StageError
        from unity_executor import UnityExecutor

        with test_directory() as project:
            (project / "Packages").mkdir()
            (project / "Packages/manifest.json").write_text('{"dependencies":{"com.unity.test-framework":"1"}}')
            cfg = SimpleNamespace(project=project, unity_exe="Unity.exe", unity_version="2022.3.62f3",
                                  unity_timeout=5, test_assemblies={"editmode": ("Game.EditMode",)})
            scope = {"assembly": "Game.EditMode", "fixtures": ["Game.ComponentTests", "Game.SecondTests"]}
            result = project / "report.xml"

            def run(argv, mode, **kwargs):
                self.assertEqual(argv[argv.index("-assemblyNames") + 1], "Game.EditMode")
                self.assertTrue(argv[argv.index("-testFilter") + 1].startswith("^"))
                self.assertEqual(argv[argv.index("-testCategory") + 1], "!GameAssetValidation")
                result.write_text('<test-run result="Passed" total="1" passed="1"><test-suite type="Assembly" '
                                  'name="Game.EditMode.dll"><test-case fullname="Game.ComponentTests.Runs" '
                                  'classname="Game.ComponentTests" result="Passed" /></test-suite></test-run>')
                return SimpleNamespace(exit_code=0)

            ctx = SimpleNamespace(artifact=lambda mode, suffix, kind: project / ("report" + suffix), run=run)
            with patch.object(UnityExecutor, "validate"), patch("unity_executor.inspect_log", return_value=(None, "2022.3.62f3", True, True)):
                with self.assertRaisesRegex(StageError, "fixture"):
                    UnityExecutor(cfg).run(ctx, "editmode", scope=scope)
                scope["fixtures"].pop()
                self.assertEqual(UnityExecutor(cfg).run(ctx, "editmode", scope=scope)["passed"], 1)
                scope["fixtures"] = ["Game"]
                with self.assertRaisesRegex(StageError, "outside the approved fixture"):
                    UnityExecutor(cfg).run(ctx, "editmode", scope=scope)
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

    def test_nunit_failed_case_label_error_is_a_fixable_test_failure(self):
        from process_runner import StageError
        from unity_executor import read_test_results

        with test_directory() as root:
            report = root / "results.xml"
            report.write_text('<test-run result="Failed" total="1" passed="0" failed="1">'
                              '<test-case fullname="Game.Fixture.Test" result="Failed" label="Error">'
                              '<failure><message>test threw</message></failure></test-case></test-run>')
            with self.assertRaises(StageError) as failure:
                read_test_results(report)
            self.assertEqual(failure.exception.kind, "tests_failed")
            self.assertEqual(failure.exception.result["failed_tests"][0]["message"], "test threw")

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
