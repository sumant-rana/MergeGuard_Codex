from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


def load_mergeguard_pr_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "mergeguard_pr.py"
    spec = importlib.util.spec_from_file_location("mergeguard_pr_script", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load mergeguard_pr.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MergeGuardPrScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = load_mergeguard_pr_module()

    def test_demo_branch_name_uses_sanitized_prefix_and_suffix(self) -> None:
        branch = self.script.make_demo_branch_name("MergeGuard Demo PR", "abc123ef")
        self.assertEqual(branch, "MergeGuard-Demo-PR-abc123ef")

    def test_demo_branch_is_prepared_when_current_branch_matches_base(self) -> None:
        args = argparse.Namespace(demo=False, no_auto_demo=False, head=None)
        self.assertTrue(
            self.script.should_prepare_demo_branch(args, current_head="main", base="main")
        )

    def test_demo_branch_is_not_prepared_for_explicit_head(self) -> None:
        args = argparse.Namespace(demo=False, no_auto_demo=False, head="feature/refund")
        self.assertFalse(
            self.script.should_prepare_demo_branch(args, current_head="main", base="main")
        )

    def test_demo_files_and_analysis_settings_are_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = self.script.write_demo_files(
                Path(tmp),
                branch="mergeguard-demo-pr-abc123ef",
                suffix="abc123ef",
            )
            prompt = Path(tmp, context.prompt_path).read_text()
            payment = Path(tmp, context.payment_path).read_text()
            self.assertIn("Ignore previous instructions", prompt)
            self.assertIn("persistCustomerEmail", payment)
            self.assertIn(context.prompt_path, context.changed_paths)

            payload = {"settings": {}}
            self.script.apply_demo_analysis_settings(payload, context)
            settings = payload["settings"]
            self.assertEqual(settings["prompt_suites"][0]["prompt_path"], context.prompt_path)
            self.assertEqual(settings["contracts"][0]["path"], context.contract_path)
            self.assertIn("@payments-team", settings["codeowners"])


if __name__ == "__main__":
    unittest.main()
