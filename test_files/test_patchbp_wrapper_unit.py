import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).parent / "mystique-opensource.github.io" / "src"
sys.path.insert(0, str(SRC))

import difftools
import llm
import patchbp
from common import ErrorCode, Language


PRE = """\
int first(void)
{
\treturn 1;
}

int second(void)
{
\treturn 2;
}
"""

POST = PRE.replace("return 1;", "return 10;").replace("return 2;", "return 20;")

TARGET = """\
/* target file comment */
int first(void)
{
\t/* keep first comment */
\treturn 1;
}

int second(void)
{
\t/* keep second comment */
\treturn 2;
}
"""


def make_patch(target=TARGET):
    return {
        "origin_before_func_code": PRE,
        "origin_after_func_code": POST,
        "target_before_func_code": target,
        "target_after_func_code": target,
    }


class WrapperTests(unittest.TestCase):
    def test_multi_method_result_is_atomic_unified_diff_for_target_path(self):
        def fake_bp(cveid, method_patch, file_path, name, language,
                    overwrite, slice_level):
            raw = method_patch["_raw_target_method_code"]
            old = "return 1;" if name == "first" else "return 2;"
            new = "return 10;" if name == "first" else "return 20;"
            return {
                "error": ErrorCode.SUCCESS.value,
                "fixed_code": raw.replace(old, new),
                "usage": llm.LLMUsage(calls=1, input_tokens=10, output_tokens=2),
            }

        with patch.object(patchbp, "bp", side_effect=fake_bp):
            result = patchbp.bp_warper(
                "case", make_patch(), "new/ref.c", "ref", Language.C,
                target_file_path="old/target.c",
            )

        self.assertEqual(ErrorCode.SUCCESS.value, result["error"])
        self.assertEqual(["first", "second"], result["modified_methods"])
        self.assertEqual(2, result["usage"].calls)
        self.assertTrue(result["fixed_code"].startswith(
            "--- a/old/target.c\n+++ b/old/target.c\n@@"
        ))
        self.assertIn("/* keep first comment */", TARGET)
        self.assertIsNotNone(difftools.normalize_and_validate_unified_diff(
            result["fixed_code"], TARGET, "old/target.c"
        ))

    def test_one_method_failure_never_returns_partial_patch(self):
        def fake_bp(cveid, method_patch, file_path, name, language,
                    overwrite, slice_level):
            if name == "second":
                return {
                    "error": ErrorCode.PDG_NOT_FOUND.value,
                    "usage": llm.LLMUsage(calls=1),
                }
            return {
                "error": ErrorCode.SUCCESS.value,
                "fixed_code": method_patch["_raw_target_method_code"].replace(
                    "return 1;", "return 10;"
                ),
                "usage": llm.LLMUsage(calls=1),
            }

        with patch.object(patchbp, "bp", side_effect=fake_bp):
            result = patchbp.bp_warper(
                "case", make_patch(), "ref.c", "ref", Language.C,
            )

        self.assertEqual(ErrorCode.PARTIAL_BACKPORT_FAILED.value, result["error"])
        self.assertEqual(ErrorCode.PDG_NOT_FOUND.value, result["cause"])
        self.assertEqual("second", result["failed_method"])
        self.assertEqual(["first"], result["completed_methods"])
        self.assertNotIn("fixed_code", result)
        self.assertEqual(2, result["usage"].calls)

    def test_missing_target_method_is_distinct_and_loud(self):
        target = TARGET.replace("second", "renamed_second")
        with patch.object(patchbp, "bp") as mocked:
            result = patchbp.bp_warper(
                "case", make_patch(target), "ref.c", "ref", Language.C,
            )
        self.assertEqual(ErrorCode.TARGET_METHOD_NOT_FOUND.value, result["error"])
        self.assertEqual("second", result["failed_method"])
        mocked.assert_not_called()

    def test_change_outside_methods_is_not_silently_dropped(self):
        patch_data = make_patch()
        patch_data["origin_before_func_code"] = "int global = 1;\n" + PRE
        patch_data["origin_after_func_code"] = "int global = 2;\n" + PRE
        with patch.object(patchbp, "bp") as mocked:
            result = patchbp.bp_warper(
                "case", patch_data, "ref.c", "ref", Language.C,
            )
        self.assertEqual(ErrorCode.CHANGE_OUTSIDE_METHOD.value, result["error"])
        mocked.assert_not_called()

    def test_diff_validator_rejects_full_source_and_bad_hunks(self):
        self.assertIsNone(difftools.normalize_and_validate_unified_diff(
            TARGET, TARGET, "target.c"
        ))
        bad = "--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-nope\n+yes\n"
        self.assertIsNone(difftools.normalize_and_validate_unified_diff(
            bad, TARGET, "target.c"
        ))


if __name__ == "__main__":
    unittest.main()
