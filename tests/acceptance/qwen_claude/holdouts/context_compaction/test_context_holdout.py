"""Independent boundary checks for the context-planner acceptance fixture."""

import copy
import unittest

from context_compaction import ContextError, compact_messages, estimate_tokens


class Holdout(unittest.TestCase):
    def test_total_forwarded_budget_includes_old_and_active_messages(self):
        source = [
            {"role": "system", "content": "Keep the policy"},
            {"role": "user", "content": "old notes " * 25},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "current work " * 12},
        ]
        self.assertGreater(estimate_tokens(source), 100)
        saved = copy.deepcopy(source)
        forwarded, audit = compact_messages(source, target=100)
        self.assertEqual(source, saved)
        self.assertLessEqual(estimate_tokens(forwarded), 100)
        self.assertTrue(audit["compacted"])
        self.assertEqual(forwarded[0], source[0])
        self.assertEqual(forwarded[-1], source[-1])

    def test_instruction_and_active_work_together_must_fit(self):
        source = [
            {"role": "system", "content": "policy " * 90},
            {"role": "user", "content": "old log " * 350},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "current task " * 25},
        ]
        with self.assertRaises(ContextError):
            compact_messages(source, target=200)

    def test_old_assistant_media_is_not_rewritten_as_text(self):
        source = [
            {"role": "user", "content": "old " * 500},
            {"role": "assistant", "content": [{"type": "image", "source": {"data": "abc"}}]},
            {"role": "user", "content": "continue"},
        ]
        with self.assertRaises(ContextError):
            compact_messages(source, target=180)


if __name__ == "__main__":
    unittest.main()
