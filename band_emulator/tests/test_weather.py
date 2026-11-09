"""Execute original firmware; expectations are independent observable contracts."""
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather_probe import run_temperature


class WeatherTests(unittest.TestCase):
    def check_run(self, kind, a, b, expected, **kwargs):
        result = run_temperature(kind, a, b, trace_limit=0, **kwargs)
        self.assertEqual(result['status'], 'returned')
        self.assertEqual(result['temperature'], expected)
        self.assertEqual(result['mmio_event_count'], 0)
        self.assertEqual(result['experiment']['mocked_functions'], [])

    def test_celsius_signed_min_max_and_input_order(self):
        rng = random.Random(2398)
        pairs = [(-3, 3), (3, -3), (0, 0), (-20, -3), (-32768, 32767), (32767, -32768)]
        pairs += [(rng.randint(-32768, 32767), rng.randint(-32768, 32767)) for _ in range(24)]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.check_run('minimum', a, b, min(a, b))
                self.check_run('maximum', a, b, max(a, b))

    def test_fahrenheit_known_points_and_fractional_results(self):
        for a, b, low, high in [(0, 100, 32, 212), (-3, 3, 27, 37), (20, 10, 50, 68), (1, 2, 34, 36)]:
            with self.subTest(a=a, b=b):
                self.check_run('minimum', a, b, low, fahrenheit=True)
                self.check_run('maximum', a, b, high, fahrenheit=True)

    def test_stock_negative_fahrenheit_rounding_is_preserved(self):
        # Observed stock behavior, NOT corrected to ideal nearest rounding.
        # Original instructions add +0.5 before truncation, even for negative F.
        self.check_run('minimum', -40, -30, -39, fahrenheit=True)
        self.check_run('maximum', -40, -30, -21, fahrenheit=True)

    def test_zero_branch_uses_fallback_and_ignores_pair(self):
        for kind in ('minimum', 'maximum'):
            self.check_run(kind, -100, 100, -2, extended=False, fallback=-2)
            self.check_run(kind, -100, 100, 28, extended=False, fallback=-2, fahrenheit=True)

    def test_input_width_is_enforced(self):
        with self.assertRaises(ValueError):
            run_temperature('minimum', -32769, 0)
        with self.assertRaises(ValueError):
            run_temperature('maximum', 0, 32768)


if __name__ == '__main__':
    unittest.main(verbosity=2)
