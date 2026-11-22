"""Keep named suites selectable from the installed domain inventory."""
import json
from pathlib import Path
import unittest

from domain_test_runner import _flatten, select_tests


class SuiteSelectionTests(unittest.TestCase):
    def test_installed_suite_prefixes_match_cases(self):
        checks = Path(__file__).resolve().parent
        profile = json.loads((checks.parents[1] / "contracts/testing-profile.json").read_text())
        for name, module in (("runner", "atlas_runner_selftest"),
                             ("integration", "atlas_efficiency_daemon_selftest"),
                             ("consistency", "atlas_consistency_selftest")):
            with self.subTest(suite=name):
                cases = unittest.defaultTestLoader.loadTestsFromName(module)
                inventory = [case.id() for case in _flatten(cases)]
                self.assertEqual(select_tests(inventory, name, [], profile["suite_prefixes"]), inventory)
