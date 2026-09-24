from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_chat_small_text_and_primary_action_token_has_stronger_contrast():
    css = (ROOT / "static" / "chat.css").read_text(encoding="utf-8")
    assert "--coral: #a94321" in css


def test_chat_action_targets_use_the_shared_44_pixel_minimum():
    css = (ROOT / "static" / "chat.css").read_text(encoding="utf-8")
    assert ".notify {\n  min-height: var(--touch-target);" in css
    assert ".bubble-actions button { min-height: var(--touch-target);" in css
    assert ".emoji-tray button { flex: 0 0 auto; min-width: var(--touch-target); min-height: var(--touch-target);" in css
    jump = css[css.index(".jump-latest {"):css.index(".message-pane { position: relative;")]
    assert "min-height: var(--touch-target);" in jump


def test_chat_stylesheet_cache_key_is_advanced():
    template = (ROOT / "templates" / "chat.html").read_text(encoding="utf-8")
    assert "/static/chat.css?v=16" in template
