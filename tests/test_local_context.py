"""Qwen provider-template translation, independent of Sift."""
from subroute.local_context import frontload_anthropic_system_messages

def test_claude_system_entry_is_frontloaded_without_changing_roles_or_content():
    user = {"role": "user", "content": [{"type": "text", "text": "Fix the file"}]}
    system = {"role": "system", "content": [{"type": "text", "text": "Use the tools"}]}
    data = {"model": "qwen3.8-27b", "messages": [user, system], "system": "Base instruction"}
    result = frontload_anthropic_system_messages(data, "anthropic_messages")
    assert result["messages"] == [system, user]
    assert result["messages"][0] == system
    assert result["system"] == "Base instruction"
    assert result["metadata"]["subroute_local_system_translation"] == "initial_system_prefix"


def test_developer_instruction_frontloads_without_losing_existing_metadata():
    # Derived from the useful boundary found by the local Claude worker.
    user = {"role": "user", "content": "Question"}
    developer = {"role": "developer", "content": "Use the tools"}
    data = {"model": "qwen3.8-27b", "messages": [user, developer], "metadata": {"trace": "keep"}}

    result = frontload_anthropic_system_messages(data, "aanthropic_messages")

    assert result["messages"] == [developer, user]
    assert result["metadata"] == {"trace": "keep", "subroute_local_system_translation": "initial_system_prefix"}


def test_chat_completions_system_order_is_not_rewritten():
    messages = [{"role": "user", "content": "Question"}, {"role": "system", "content": "Later instruction"}]
    data = {"model": "qwen3.8-27b", "messages": messages}
    assert frontload_anthropic_system_messages(data, "acompletion")["messages"] is messages
