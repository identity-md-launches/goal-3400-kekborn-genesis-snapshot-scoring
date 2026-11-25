#!/usr/bin/env python3
"""Fixture tests for score.py (standard library). Run: python3 tools/scores/test_score.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score as S
assert S.jenks([(1, 1), (2, 1), (3, 1)], 5) == [1, 2, 3]
assert S.jenks([(1, 3), (2, 1), (10, 1), (11, 2), (50, 1), (51, 1), (90, 1), (100, 2)], 5) == [2, 11, 51, 90, 100]
assert S.tier_of(0, [5]) == "Common" and S.tier_of(5, [5]) == "Legendary"
assert S.tier_of(1, [1, 2]) == "Epic"
z = {"completed": 0, "imd_spent": 0, "diversity": 0, "complexity": 0, "deliverables": 0, "quality": 0, "goals": 0}
assert S.score_of(z)[0] == 0
big = {k: 10**9 for k in z}; big["diversity"] = 6
assert S.score_of(big)[0] == 100
print("test_score: all passed")
