from copy import deepcopy

from sift import compact_messages


def _word_count(messages):
    def words(value):
        if isinstance(value, str):
            return len(value.split())
        if isinstance(value, dict):
            return sum(words(key) + words(item) for key, item in value.items())
        if isinstance(value, list):
            return sum(words(item) for item in value)
        return 0

    return words(messages)


def _cycle(call_id, argument, result):
    return [
        {"role": "assistant", "tool_calls": [{
            "id": call_id, "type": "function", "function": {
                "name": "lookup", "arguments": argument,
            },
        }]},
        {"role": "tool", "tool_call_id": call_id, "name": "lookup", "content": result},
    ]


def test_latest_tool_cycle_recovers_without_protecting_old_call_arguments():
    messages = [{"role": "system", "content": "Keep exact instructions."},
                {"role": "user", "content": "Find the latest result."}]
    for index in range(4):
        messages += _cycle(f"old{index}", " ".join([f"argument{index}"] * 700), "old result")
    latest_arguments = '{"query":"latest item"}'
    latest = _cycle("latest42", latest_arguments, " ".join(["latestresult"] * 2500))
    messages += latest
    original = deepcopy(messages)

    result = compact_messages(messages, _word_count, target=2600, raw_limit=10_000)

    assert _word_count(result.messages) <= 2600
    assert result.messages[0] == original[0]
    assert result.messages[1] == original[1]
    latest_call = next(message for message in result.messages
                       if message.get("role") == "assistant"
                       and message.get("tool_calls", [{}])[0].get("id") == "latest42")
    assert latest_call == latest[0]
    latest_result = next(message for message in result.messages
                         if message.get("role") == "tool"
                         and message.get("tool_call_id") == "latest42")
    assert latest_result["name"] == "lookup"
    assert "lossy" in latest_result["content"]
    assert len(latest_result["content"]) < len(latest[1]["content"])
    assert "latestresult" in latest_result["content"]
    assert messages == original
    assert result.audit["active_tool_results_compacted"] == 1


def test_anthropic_latest_tool_result_is_compacted_without_changing_tool_ids():
    messages = [
        {"role": "system", "content": "Keep exact instructions."},
        {"role": "user", "content": "Find the latest failure evidence."},
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "read-latest", "name": "lookup",
            "input": {"query": "latest failure"},
        }]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "read-latest",
            "content": [{"type": "text", "text": "latest failure evidence " * 5_000}],
        }]},
    ]
    original = deepcopy(messages)

    result = compact_messages(
        messages, _word_count, target=700, raw_limit=30_000, protocol="anthropic",
    )

    assert _word_count(result.messages) <= 700
    assert result.messages[0:3] == original[0:3]
    tool_result = result.messages[3]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "read-latest"
    assert "lossy" in tool_result["content"]
    assert "latest failure" in tool_result["content"]
    assert messages == original
    assert result.audit["active_tool_results_compacted"] == 1
