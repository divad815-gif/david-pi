from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_assistant_marks_long_running_and_history_requests_busy():
    template = (ROOT / "templates" / "assistant.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "assistant.js").read_text(encoding="utf-8")

    assert 'id="chatLog"' in template and 'aria-busy="false"' in template
    assert 'id="conversationList" aria-busy="false"' in template
    assert "log.setAttribute('aria-busy', 'true')" in script
    assert "conversationList.setAttribute('aria-busy', 'true')" in script


def test_assistant_ignores_answers_after_conversation_changes():
    script = (ROOT / "static" / "assistant.js").read_text(encoding="utf-8")

    assert "const requestedConversation = conversationId" in script
    assert "requestedConversation !== conversationId" in script
    assert "activeAnswer?.abort()" in script
    assert "problem.name === 'AbortError'" in script


def test_assistant_history_has_loading_empty_error_and_current_states():
    template = (ROOT / "templates" / "assistant.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "assistant.js").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "assistant.css").read_text(encoding="utf-8")

    assert 'id="historyState" role="status" aria-live="polite"' in template
    assert "Loading conversations…" in script
    assert "No saved conversations yet." in script
    assert "Conversation history could not be loaded." in script
    assert "button.setAttribute('aria-current', 'true')" in script
    assert '#conversationList > button[aria-current="true"]' in styles


def test_assistant_history_identifies_household_creators_and_message_senders():
    template = (ROOT / "templates" / "assistant.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "assistant.js").read_text(encoding="utf-8")

    assert "Shared household history for Household." in template
    assert "Started by ${conversation.creator_name || 'Household member'}" in script
    assert "item.mine ? 'You'" in script
    assert "item.sender_name || 'Household member'" in script
    assert "role === 'system' ? 'System' : (window.davidPiServerName || 'Home server')" in script


def test_assistant_assets_are_independently_cache_busted():
    template = (ROOT / "templates" / "assistant.html").read_text(encoding="utf-8")

    assert '/static/assistant.css?v=3' in template
    assert '/static/assistant.js?v=10' in template
