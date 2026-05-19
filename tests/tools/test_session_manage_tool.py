import asyncio

from nanobot.agent.tools.session_manage import SessionManageTool
from nanobot.session.manager import SessionManager


def test_session_manage_create_switch_current_and_reset(tmp_path) -> None:
    manager = SessionManager(tmp_path / "workspace")
    tool = SessionManageTool(manager)
    tool.set_context("feishu", "oc_chat")

    created = asyncio.run(
        tool.execute(
            action="create",
            session_key="project alpha",
            title="Project Alpha",
        )
    )

    expected_key = "feishu:oc_chat:project-alpha"
    assert "Created and activated session" in created
    assert expected_key in created
    assert manager.get_active_session_key("feishu", "oc_chat") == expected_key

    current = asyncio.run(tool.execute(action="current"))
    assert expected_key in current
    assert "Project Alpha" in current

    reset = asyncio.run(tool.execute(action="reset"))
    assert "Cleared active session override" in reset
    assert manager.get_active_session_key("feishu", "oc_chat") is None

    switched = asyncio.run(tool.execute(action="switch", session_key="project alpha"))
    assert "Switched active session" in switched
    assert manager.get_active_session_key("feishu", "oc_chat") == expected_key


def test_session_manage_list_marks_active_session(tmp_path) -> None:
    manager = SessionManager(tmp_path / "workspace")
    tool = SessionManageTool(manager)
    tool.set_context("feishu", "oc_chat")

    asyncio.run(tool.execute(action="create", session_key="one", title="One"))
    asyncio.run(tool.execute(action="create", session_key="two", title="Two"))

    listed = asyncio.run(tool.execute(action="list", limit=10))

    assert "Sessions:" in listed
    assert "* feishu:oc_chat:two" in listed
    assert "feishu:oc_chat:one" in listed
