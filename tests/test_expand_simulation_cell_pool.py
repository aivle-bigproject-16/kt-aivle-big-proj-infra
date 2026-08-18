import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "expand-simulation-cell-pool.py"
SPEC = importlib.util.spec_from_file_location("expand_simulation_cell_pool", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def recorded_group(size=20):
    return [f"SIM-{index:04d}" for index in range(1, size + 1)]


class BuildClonePlanTest(unittest.TestCase):
    def test_plan_is_empty_when_target_is_already_satisfied(self):
        plan = MODULE.build_clone_plan(recorded_group(), 20)
        self.assertEqual(plan, [])

    def test_group_is_consumed_without_replacement_then_resets(self):
        plan = MODULE.build_clone_plan(recorded_group(), 60)

        self.assertEqual(len(plan), 40)
        self.assertEqual(plan[0], ("SIM-0021", "SIM-0001"))
        self.assertEqual(plan[19], ("SIM-0040", "SIM-0020"))
        self.assertEqual(plan[20], ("SIM-0041", "SIM-0001"))
        self.assertEqual(plan[39], ("SIM-0060", "SIM-0020"))

        new_serials = [serial for serial, _ in plan]
        self.assertEqual(len(new_serials), len(set(new_serials)))
        for group_start in range(0, len(plan), 20):
            group = [template for _, template in plan[group_start:group_start + 20]]
            self.assertEqual(sorted(group), recorded_group())

    def test_existing_gaps_are_filled_before_new_serials(self):
        existing = ["SIM-0001", "SIM-0002", "SIM-0004"]
        plan = MODULE.build_clone_plan(existing, 5, group_size=2)

        self.assertEqual(plan, [("SIM-0003", "SIM-0001"), ("SIM-0005", "SIM-0001")])

    def test_group_size_limits_the_templates(self):
        plan = MODULE.build_clone_plan(recorded_group(), 23, group_size=2)

        self.assertEqual(
            plan,
            [
                ("SIM-0021", "SIM-0001"),
                ("SIM-0022", "SIM-0002"),
                ("SIM-0023", "SIM-0001"),
            ],
        )

    def test_rejects_empty_pool_and_invalid_target(self):
        with self.assertRaises(ValueError):
            MODULE.build_clone_plan([], 10)
        with self.assertRaises(ValueError):
            MODULE.build_clone_plan(recorded_group(), 0)


if __name__ == "__main__":
    unittest.main()
