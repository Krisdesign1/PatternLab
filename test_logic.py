import unittest
from analytics import (
    complete_patterns, subsequence_patterns,
    next_value_distribution, transition_matrix
)

TRAINING = [
    ("2nd","2nd","1st","Égal"),
    ("Égal","1st","Égal","Égal"),
    ("1st","2nd","2nd","1st"),
    ("Égal","1st","Égal","Égal"),
    ("Égal","2nd","1st","1st"),
    ("Égal","1st","Égal","1st"),
]

class TestPatternLab(unittest.TestCase):
    def test_complete_repeat(self):
        c = complete_patterns(TRAINING, 4)
        self.assertEqual(c[("Égal","1st","Égal","Égal")], 2)

    def test_subsequence_repeat(self):
        c = subsequence_patterns(TRAINING, 3)
        self.assertEqual(c[("Égal","1st","Égal")], 3)

    def test_next_distribution(self):
        c, n = next_value_distribution(TRAINING, ("Égal","1st","Égal"))
        self.assertEqual(n, 3)
        self.assertEqual(c["Égal"], 2)
        self.assertEqual(c["1st"], 1)

    def test_no_cross_coupon(self):
        c = subsequence_patterns([("1st","1st"),("2nd","2nd")], 2)
        self.assertNotIn(("1st","2nd"), c)

if __name__ == "__main__":
    unittest.main()
