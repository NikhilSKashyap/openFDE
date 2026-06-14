"""
Tests for openfde.agent_settings — focused on the per-role customPrompt (Council
chat instructions): it normalizes, merges, round-trips to the public shape, is capped,
and is NOT a secret (it shows in to_public unmasked, unlike apiKey).
"""
import unittest

from openfde import agent_settings as A


class CustomPromptTest(unittest.TestCase):
    def test_normalize_keeps_custom_prompt(self):
        cfg = A.normalize_role({"provider": "anthropic", "customPrompt": "Be terse."})
        self.assertEqual(cfg["customPrompt"], "Be terse.")

    def test_default_custom_prompt_is_empty(self):
        self.assertEqual(A.normalize_role({})["customPrompt"], "")

    def test_custom_prompt_is_capped(self):
        cfg = A.normalize_role({"customPrompt": "x" * 5000})
        self.assertLessEqual(len(cfg["customPrompt"]), 2000)

    def test_merge_updates_custom_prompt_and_preserves_key(self):
        base = A.normalize({"architect": {"provider": "anthropic", "apiKey": "sk-secret",
                                          "model": "m", "customPrompt": "old"}})
        merged = A.merge(base, {"architect": {"customPrompt": "new style"}})
        self.assertEqual(merged["architect"]["customPrompt"], "new style")
        self.assertEqual(merged["architect"]["apiKey"], "sk-secret")    # key preserved

    def test_to_public_exposes_custom_prompt_but_not_the_key(self):
        pub = A.to_public({"verifier": {"provider": "anthropic", "apiKey": "sk-zzz",
                                        "model": "m", "customPrompt": "cite tests"}})
        self.assertEqual(pub["verifier"]["customPrompt"], "cite tests")  # not a secret
        self.assertNotIn("apiKey", pub["verifier"])                      # secret stripped
        self.assertTrue(pub["verifier"]["hasApiKey"])


if __name__ == "__main__":
    unittest.main()
