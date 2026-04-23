from nanobot.session.manager import Session


def _assistant_tool_call(call_id: str, name: str = "read_file") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": "{}",
        },
    }


def test_get_history_separates_dialog_and_tool_limits() -> None:
    session = Session(key="cli:test")

    session.add_message("user", "u1")
    session.add_message("assistant", "a1")
    session.add_message("assistant", "", tool_calls=[_assistant_tool_call("tc1")])
    session.add_message("tool", "tool-result-1", tool_call_id="tc1", name="read_file")
    session.add_message("user", "u2")
    session.add_message("assistant", "a2")
    session.add_message("assistant", "", tool_calls=[_assistant_tool_call("tc2", name="list_dir")])
    session.add_message("tool", "tool-result-2", tool_call_id="tc2", name="list_dir")
    session.add_message("user", "u3")
    session.add_message("assistant", "a3")

    history = session.get_history(
        max_messages=50,
        max_dialog_messages=3,
        max_tool_messages=2,
    )

    roles = [m["role"] for m in history]
    assert roles == ["assistant", "assistant", "tool", "user", "assistant"]

    dialog_contents = [m["content"] for m in history if m["role"] in {"user", "assistant"} and "tool_calls" not in m]
    assert dialog_contents == ["a2", "u3", "a3"]

    tool_assistant = [m for m in history if m["role"] == "assistant" and "tool_calls" in m]
    assert len(tool_assistant) == 1
    assert tool_assistant[0]["tool_calls"][0]["id"] == "tc2"

    tool_messages = [m for m in history if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "tc2"


def test_get_history_repairs_unpaired_tool_message_after_separate_limit() -> None:
    session = Session(key="cli:test-repair")

    session.add_message("assistant", "", tool_calls=[_assistant_tool_call("tc1")])
    session.add_message("tool", "tool-result-1", tool_call_id="tc1", name="read_file")
    session.add_message("user", "u1")
    session.add_message("assistant", "a1")

    history = session.get_history(
        max_messages=50,
        max_dialog_messages=2,
        max_tool_messages=1,
    )

    # max_tool_messages=1 keeps only the latest tool-related message before repair,
    # so the tool message without its assistant tool_calls envelope must be dropped.
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert [m["content"] for m in history] == ["u1", "a1"]


def test_get_history_skips_timeout_events_without_breaking_tool_pairs() -> None:
    session = Session(key="cli:test-timeout-events")

    session.add_message("assistant", "", tool_calls=[_assistant_tool_call("tc1")])
    session.add_message("tool", "tool-result-1", tool_call_id="tc1", name="read_file")
    session.add_message("assistant", "final-answer")

    # Timeout transport events are persisted for audit, but excluded from model context.
    session.add_message(
        "assistant",
        "stream timeout notice",
        include_in_context=False,
        event_type="streaming_timeout",
    )
    session.add_message(
        "assistant",
        "fallback continuation",
        include_in_context=False,
        event_type="timeout_fallback",
    )

    history = session.get_history(
        max_messages=50,
        max_dialog_messages=5,
        max_tool_messages=5,
    )

    assert [m["role"] for m in history] == ["assistant", "tool", "assistant"]
    assert history[0]["tool_calls"][0]["id"] == "tc1"
    assert history[1]["tool_call_id"] == "tc1"
    assert history[2]["content"] == "final-answer"
