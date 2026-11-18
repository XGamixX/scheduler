#!/usr/bin/env python3
"""Synthetic unit tests for the directional-delay calibration equations."""

import unittest

from scheduler import Sample, directional_deviation_samples


class DirectionalMathTests(unittest.TestCase):
    def make_sample(self, delay_ms: int, offset_ms: int) -> Sample:
        return Sample(
            server="synthetic",
            offset_ns=offset_ms * 1_000_000,
            delay_ns=delay_ms * 1_000_000,
            root_distance_ns=0,
            wall_mid_ns=0,
            mono_mid_ns=0,
        )

    def test_forward_only_increase(self):
        samples = [
            self.make_sample(40, 3),
            self.make_sample(50, 8),
        ]
        forward, reverse = directional_deviation_samples(samples)
        self.assertEqual(forward, [-5_000_000, 5_000_000])
        self.assertEqual(reverse, [0, 0])

    def test_symmetric_increase(self):
        samples = [
            self.make_sample(40, 3),
            self.make_sample(50, 3),
        ]
        forward, reverse = directional_deviation_samples(samples)
        self.assertEqual(forward, [-2_500_000, 2_500_000])
        self.assertEqual(reverse, [-2_500_000, 2_500_000])

    def test_reverse_only_increase(self):
        samples = [
            self.make_sample(40, 3),
            self.make_sample(50, -2),
        ]
        forward, reverse = directional_deviation_samples(samples)
        self.assertEqual(forward, [0, 0])
        self.assertEqual(reverse, [-5_000_000, 5_000_000])


if __name__ == "__main__":
    unittest.main(verbosity=2)
