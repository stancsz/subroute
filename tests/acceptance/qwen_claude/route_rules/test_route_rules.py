import unittest

from route_rules import resolve_model


class RouteRulesTests(unittest.TestCase):
    def test_auto_group_stays_auto_under_every_policy(self):
        for mode in ("off", "alias", "force"):
            with self.subTest(mode=mode):
                self.assertEqual(resolve_model("auto", "qwen3.8-27b", mode), "auto")

    def test_force_routes_an_explicit_model(self):
        self.assertEqual(resolve_model("minimax", "qwen3.8-27b", "force"), "qwen3.8-27b")

    def test_alias_mode_only_rewrites_virtual_aliases(self):
        self.assertEqual(resolve_model("current", "qwen3.8-27b", "alias"), "qwen3.8-27b")
        self.assertEqual(resolve_model("default", "qwen3.8-27b", "alias"), "qwen3.8-27b")
        self.assertEqual(resolve_model("minimax", "qwen3.8-27b", "alias"), "minimax")

    def test_off_preserves_every_request(self):
        for requested in ("current", "auto", "minimax"):
            with self.subTest(requested=requested):
                self.assertEqual(resolve_model(requested, "qwen3.8-27b", "off"), requested)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_model("minimax", "qwen3.8-27b", "guess")


if __name__ == "__main__":
    unittest.main()
