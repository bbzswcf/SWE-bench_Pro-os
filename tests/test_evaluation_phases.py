import unittest

from swe_bench_pro_eval import (
    TIMING_SENSITIVE_INSTANCE_IDS,
    plan_evaluation_phases,
)


class PlanEvaluationPhasesTest(unittest.TestCase):
    def setUp(self):
        self.sensitive_id = next(iter(TIMING_SENSITIVE_INSTANCE_IDS))
        self.regular_patch = {"instance_id": "regular-instance"}
        self.sensitive_patch = {"instance_id": self.sensitive_id}
        self.patches = [self.sensitive_patch, self.regular_patch]

    def test_requested_concurrency_below_four_keeps_one_phase(self):
        self.assertEqual(
            plan_evaluation_phases(self.patches, 3),
            [("all instances", self.patches, 3)],
        )

    def test_requested_concurrency_at_least_four_splits_sequential_phases(self):
        self.assertEqual(
            plan_evaluation_phases(self.patches, 20),
            [
                ("regular instances", [self.regular_patch], 20),
                ("timing-sensitive instances", [self.sensitive_patch], 4),
            ],
        )

    def test_requested_concurrency_four_also_splits(self):
        self.assertEqual(
            plan_evaluation_phases(self.patches, 4),
            [
                ("regular instances", [self.regular_patch], 4),
                ("timing-sensitive instances", [self.sensitive_patch], 4),
            ],
        )

    def test_empty_phase_is_omitted(self):
        self.assertEqual(
            plan_evaluation_phases([self.sensitive_patch], 30),
            [("timing-sensitive instances", [self.sensitive_patch], 4)],
        )

    def test_non_positive_concurrency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least 1"):
            plan_evaluation_phases(self.patches, 0)


if __name__ == "__main__":
    unittest.main()
