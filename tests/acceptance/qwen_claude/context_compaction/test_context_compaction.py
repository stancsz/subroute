import copy
import unittest

from context_compaction import ContextError, compact_messages, estimate_tokens


def msg(role, content):
    return {"role": role, "content": content}


class ContextCompactionTests(unittest.TestCase):
    def test_small_request_is_unchanged_and_not_mutated(self):
        original = [msg("system", "policy"), msg("user", "hello")]
        saved = copy.deepcopy(original)
        forwarded, audit = compact_messages(original, target=100)
        self.assertEqual(forwarded, saved)
        self.assertEqual(original, saved)
        self.assertEqual(audit, {"input_tokens": estimate_tokens(saved),
                                 "forwarded_tokens": estimate_tokens(saved),
                                 "omitted_tokens": 0, "compacted": False})

    def test_preserves_instructions_and_active_tool_chain(self):
        old = [msg("user", "old request " * 450), msg("assistant", "old answer " * 450)]
        active = [msg("user", "solve the bug"),
                  msg("assistant", [{"type": "tool_use", "id": "call-1", "input": {"command": "test"}}]),
                  msg("user", [{"type": "tool_result", "tool_use_id": "call-1", "content": "FAILED"}])]
        original = [msg("system", "immutable policy"), msg("developer", "route locally")] + old + active
        saved = copy.deepcopy(original)
        forwarded, audit = compact_messages(original, target=420)
        self.assertEqual(original, saved)
        self.assertEqual(forwarded[:2], saved[:2])
        self.assertEqual(forwarded[-3:], active)
        self.assertEqual(len(forwarded), 6)
        self.assertEqual(forwarded[2]["role"], "user")
        self.assertIn("historical", forwarded[2]["content"].lower())
        self.assertLessEqual(estimate_tokens(forwarded), 420)
        self.assertTrue(audit["compacted"])
        self.assertEqual(audit["input_tokens"], estimate_tokens(saved))
        self.assertEqual(audit["forwarded_tokens"], estimate_tokens(forwarded))
        self.assertEqual(audit["omitted_tokens"], audit["input_tokens"] - audit["forwarded_tokens"])

    def test_keeps_active_user_when_last_user_message_is_tool_result(self):
        original = [msg("user", "old " * 1000), msg("assistant", "answer " * 1000),
                    msg("user", "current task"), msg("assistant", "working"),
                    msg("user", [{"type": "tool_result", "tool_use_id": "x", "content": "test failed"}])]
        forwarded, _ = compact_messages(original, target=300)
        self.assertEqual(forwarded[-3:], original[-3:])

    def test_excerpt_retains_representative_old_content(self):
        original = [msg("user", "EARLY_DECISION " + "details " * 500),
                    msg("assistant", "details " * 500 + "LATE_FINDING"),
                    msg("user", "continue")]
        forwarded, _ = compact_messages(original, target=220)
        excerpt = forwarded[0]["content"]
        self.assertIn("EARLY_DECISION", excerpt)
        self.assertIn("LATE_FINDING", excerpt)

    def test_rejects_oversize_or_irreducible_active_turn(self):
        original = [msg("user", "old " * 500), msg("assistant", "answer"), msg("user", "new " * 500)]
        with self.assertRaisesRegex(ContextError, "raw|limit"):
            compact_messages(original, target=120, raw_limit=200)
        with self.assertRaisesRegex(ContextError, "active|fit|target"):
            compact_messages(original, target=120)

    def test_rejects_late_instruction_and_media_when_compacting(self):
        prior = [msg("user", "old " * 400), msg("assistant", "answer " * 400)]
        with self.assertRaisesRegex(ContextError, "instruction|system"):
            compact_messages(prior + [msg("system", "late rule"), msg("user", "now")], target=200)
        with self.assertRaisesRegex(ContextError, "media|content|unsupported"):
            compact_messages(prior + [msg("user", [{"type": "image", "url": "x"}])], target=200)


if __name__ == "__main__":
    unittest.main()
