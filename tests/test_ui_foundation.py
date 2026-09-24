import json
import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
MAJOR_TEMPLATES = {
    "assistant.html", "audiobooks.html", "chat.html", "device_backup.html",
    "files.html", "games.html", "home.html", "movies.html", "notes.html",
    "mytube.html", "mytube_watch.html", "photo_collections.html", "photos.html",
    "places.html", "recipes.html", "recipe_weekly_plan.html", "status.html", "settings.html",
}


class ContractParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.dialogs = []
        self.navs = []
        self.controls = []
        self.buttons = []
        self.labels_for = set()
        self.scripts = []
        self.label_depth = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "label":
            self.label_depth += 1
            if values.get("for"):
                self.labels_for.add(values["for"])
        elif tag == "dialog" or values.get("role") == "dialog":
            self.dialogs.append(values)
        elif tag == "nav":
            self.navs.append(values)
        elif tag in {"input", "select", "textarea"}:
            self.controls.append((values, self.label_depth > 0))
        elif tag == "button":
            self.buttons.append(values)
        elif tag == "script" and values.get("src"):
            self.scripts.append(values["src"])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == "label":
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "label":
            self.label_depth = max(0, self.label_depth - 1)


def parsed_templates():
    for path in sorted(TEMPLATES.glob("*.html")):
        parser = ContractParser()
        parser.feed(path.read_text())
        yield path, parser


def test_all_major_routes_load_the_shared_ui_foundation():
    seen = set()
    for path, parser in parsed_templates():
        seen.add(path.name)
        assert "/static/david-pi-ui.js?v=15" in parser.scripts, path.name
        if parser.dialogs:
            ui_index = parser.scripts.index("/static/david-pi-ui.js?v=15")
            host_index = parser.scripts.index("/static/mobile-dialog-host.js?v=10")
            assert ui_index < host_index, path.name
    assert seen == MAJOR_TEMPLATES


def test_dialogs_and_navigation_landmarks_have_resolvable_names():
    for path, parser in parsed_templates():
        for element in parser.dialogs + parser.navs:
            assert element.get("aria-label") or element.get("aria-labelledby"), (
                path.name,
                element.get("id"),
            )
            for target in element.get("aria-labelledby", "").split():
                assert target in parser.ids, (path.name, element.get("id"), target)
            for target in element.get("aria-describedby", "").split():
                assert target in parser.ids, (path.name, element.get("id"), target)


def test_template_form_controls_do_not_rely_on_placeholders_for_names():
    for path, parser in parsed_templates():
        for control, nested_in_label in parser.controls:
            if control.get("type") == "hidden":
                continue
            named = (
                nested_in_label
                or control.get("id") in parser.labels_for
                or bool(control.get("aria-label"))
                or bool(control.get("aria-labelledby"))
            )
            assert named, (path.name, control.get("id"))


def test_template_buttons_declare_their_form_behavior():
    for path, parser in parsed_templates():
        for button in parser.buttons:
            assert button.get("type") in {"button", "submit", "reset"}, (
                path.name,
                button.get("id"),
            )
            filter_keys = {
                "data-kind", "data-library-meal", "data-meal", "data-owner",
                "data-tab", "data-type", "data-view",
            }
            if filter_keys.intersection(button):
                assert button.get("aria-pressed") in {"true", "false"}, (
                    path.name,
                    button.get("id"),
                )

    offline_path = ROOT / "static" / "audiobooks-offline.html"
    offline = ContractParser()
    offline.feed(offline_path.read_text())
    for button in offline.buttons:
        assert button.get("type") in {"button", "submit", "reset"}, (
            offline_path.name,
            button.get("id"),
        )


def test_generated_buttons_declare_their_form_behavior():
    implicit_button = re.compile(r"<button(?![^>]*\btype=)", re.IGNORECASE)
    for path in sorted((ROOT / "static").glob("*.js")):
        assert not implicit_button.search(path.read_text()), path.name


def test_shared_accessibility_tokens_are_versioned_consistently():
    for path in TEMPLATES.glob("*.html"):
        source = path.read_text()
        if "chat.css" in source:
            assert "/static/chat.css?v=16" in source
        else:
            assert "/static/app.css?v=40" in source
        assert "/static/david-pi-ui.css?v=16" in source
        assert source.rindex('/static/david-pi-ui.css?v=16') > source.rindex('rel="stylesheet"'), path.name
    css = (ROOT / "static" / "app.css").read_text()
    assert "--touch-target: 44px" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "forced-colors: active" in css


def test_parser_authored_system_theme_precedes_bootstrap_on_every_surface():
    surfaces = [*TEMPLATES.glob("*.html"), ROOT / "static" / "audiobooks-offline.html"]
    for path in surfaces:
        source = path.read_text()
        theme = source.index('<meta name="theme-color" content="#fffaf2">')
        scheme = source.index('<meta name="color-scheme"')
        bootstrap = source.index('/static/theme-bootstrap.js?v=8')
        assert theme < scheme < bootstrap < source.index('rel="stylesheet"'), path.name
        assert source.count('name="theme-color"') == 1, path.name
        assert '<meta name="theme-color" content="#fffaf2">' in source, path.name
        assert 'media="(prefers-color-scheme:' not in source, path.name
        assert 'viewport-fit=cover' not in source, path.name
        assert 'viewport-fit=auto' in source, path.name
        assert '<noscript><meta name="theme-color"' not in source, path.name
    bootstrap = (ROOT / "static" / "theme-bootstrap.js").read_text()
    shared = (ROOT / "static" / "david-pi-ui.js").read_text()
    assert "davidPiThemeV1" in bootstrap and "davidPiThemeV1" in shared
    assert "mountThemeControl" in shared and "role', 'radiogroup'" in shared
    assert "metadata('theme-color')" in bootstrap
    assert "metadata('color-scheme')" in bootstrap
    assert "black-translucent" in bootstrap and "#0f1518" in bootstrap
    assert "document.createElement" not in bootstrap
    assert "data-davidpi-runtime-theme" not in bootstrap
    assert "controller.apply(value, {persist})" in shared
    assert "querySelectorAll?.('meta[name=\"theme-color\"]')" not in shared


def test_manifest_is_install_fallback_while_parser_metadata_owns_page_theme():
    manifest = json.loads((ROOT / "static" / "manifest.webmanifest").read_text())
    assert manifest["theme_color"] == "#fffaf2"
    assert manifest["background_color"] == "#fffaf2"
    for path in TEMPLATES.glob("*.html"):
        source = path.read_text()
        if 'rel="manifest"' in source:
            assert source.index('/static/theme-bootstrap.js?v=8') < source.index('rel="manifest"'), path.name
    assert "color_scheme_dark" not in manifest


def test_dark_theme_uses_semantic_surfaces_instead_of_visual_inversion():
    styles = (ROOT / "static" / "david-pi-ui.css").read_text()
    assert "filter: invert" not in styles
    for token in (
        "--night-canvas", "--night-surface", "--night-raised",
        "--night-control", "--night-text", "--night-muted",
        "--night-accent", "--night-accent-ink",
    ):
        assert token in styles
    assert ':root[data-theme="dark"] .home-group' in styles
    assert ':root[data-theme="dark"] .action-primary' in styles
    assert ':root[data-theme="dark"] .mytube-hero' in styles
    assert ':root[data-theme="dark"] .audiobook-player-sheet' in styles

    def luminance(hex_color):
        values = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        values = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in values]
        return .2126 * values[0] + .7152 * values[1] + .0722 * values[2]

    def contrast(first, second):
        high, low = sorted((luminance(first), luminance(second)), reverse=True)
        return (high + .05) / (low + .05)

    assert contrast("#f1f4f2", "#182228") >= 7
    assert contrast("#b7c1bd", "#182228") >= 4.5
    assert contrast("#28180f", "#f0aa80") >= 7


def test_dark_status_states_resolve_to_accessible_semantic_pairs():
    """Resolve the final dark custom-property values used by conditional UI states."""
    shared = (ROOT / "static" / "david-pi-ui.css").read_text()
    dark_block = shared[shared.index(':root[data-theme="dark"]'):]

    def token(name):
        match = re.search(rf"{re.escape(name)}\s*:\s*(#[0-9a-f]{{6}})\s*;", dark_block, re.I)
        assert match, name
        return match.group(1)

    def luminance(hex_color):
        values = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        values = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in values]
        return .2126 * values[0] + .7152 * values[1] + .0722 * values[2]

    def contrast(first, second):
        high, low = sorted((luminance(first), luminance(second)), reverse=True)
        return (high + .05) / (low + .05)

    for state in ("success", "warning", "danger"):
        assert contrast(token(f"--{state}-text"), token(f"--{state}-surface")) >= 4.5

    paired_rules = "\n".join(
        (ROOT / "static" / name).read_text()
        for name in ("app.css", "audiobooks-offline.css", "chat.css", "games.css", "platform.css")
    )
    for selector, text_token, surface_token in (
        (".offline-connection", "--success-text", "--success-surface"),
        (".offline-connection.is-offline", "--warning-text", "--warning-surface"),
        (".notify.ready", "--success-text", "--success-surface"),
        (".form-error", "--danger-text", "--danger-surface"),
        (".health-action", "--warning-text", "--warning-surface"),
        (".module-page-error", "--warning-text", "--warning-surface"),
        (".backup-state.warning", "--warning-text", "--warning-surface"),
    ):
        matching_rules = re.findall(rf"[^{{}}]*{re.escape(selector)}[^{{}}]*\{{([^{{}}]+)\}}", paired_rules)
        assert any(text_token in rule and surface_token in rule for rule in matching_rules), selector

    dark_rules = dark_block
    warning_rule = re.search(r"\.backup-state\.warning\s*\{([^{}]+)\}", dark_rules)
    assert warning_rule
    assert "--warning-text" in warning_rule.group(1)
    assert "--warning-surface" in warning_rule.group(1)


def test_module_backgrounds_use_shared_semantic_surface_tokens():
    forbidden = re.compile(
        r"background(?:-color)?\s*:\s*(?:white|#fff(?:fff)?|#(?:"
        r"fffaf2|fff8ee|fff4e[8d-f]|fff3e8|fff0(?:c5|d7|df|e8)|"
        r"fff1(?:ca|cf|df|ec)|f[0-9a-f]{2}(?:e[0-9a-f]{2}|d[0-9a-f]{2})|"
        r"e(?:ee7de|eeae3|ee6da|8e0d4|7ded2|4f4e7|8f4e7))"
        r")\s*[;}]",
        re.IGNORECASE,
    )
    for path in sorted((ROOT / "static").glob("*.css")):
        matches = forbidden.findall(path.read_text())
        assert not matches, (path.name, matches)

    shared = (ROOT / "static" / "david-pi-ui.css").read_text()
    for token in (
        "--canvas", "--surface", "--surface-raised", "--surface-subtle",
        "--surface-translucent", "--control-muted", "--accent-subtle",
        "--success-surface", "--warning-surface", "--danger-surface",
        "--success-text", "--warning-text", "--danger-text", "--media-canvas",
    ):
        assert token in shared
    platform = (ROOT / "static" / "platform.css").read_text()
    assert platform.count("background:var(--media-canvas)") >= 3


def test_dark_theme_overrides_known_hardcoded_light_surfaces_after_module_css():
    styles = (ROOT / "static" / "david-pi-ui.css").read_text()
    dark = styles[styles.index(':root[data-theme="dark"]'):]
    for selector in (
        ".folder-card", ".file-card", ".files-load-state",
        ".movie-card", ".movie-search-results article", ".subscription-list label",
        ".offline-player", ".offline-audiobook-card", ".offline-readiness",
        ".pair-card", ".device-card", ".recipe-card", ".restaurant-card",
    ):
        assert selector in dark, selector
    for control in (
        ".chip-nav button.selected", ".media-smart-views button.selected",
        ".movie-actions button", ".offline-remove-all", ".pane-title button",
        ".audiobook-offline-entry a", ".assistant-orb", ".backup-danger",
        ':root[data-theme="dark"] .game-picker button.selected',
    ):
        assert control in dark, control


def test_mobile_sheet_actions_match_the_twenty_pixel_sheet_gutter():
    css = (ROOT / "static" / "app.css").read_text()
    assert ".small-sheet > .dialog-actions { position: sticky; bottom: -28px;" in css
    assert "margin: 14px -20px -28px; padding: 12px 20px" in css
    assert "margin: 14px -28px -28px" not in css

    affected_sheets = {
        "photos.html": ("slideshowSheet", "organizeSheet"),
        "recipes.html": ("recipeEditor",),
        "files.html": ("folderSheet",),
        "audiobooks.html": ("editAudiobookSheet",),
    }
    for template_name, sheet_ids in affected_sheets.items():
        source = (TEMPLATES / template_name).read_text()
        for sheet_id in sheet_ids:
            start = source.index(f'id="{sheet_id}"')
            assert 'class="dialog-actions"' in source[start:], (template_name, sheet_id)


def test_offline_shelf_also_loads_the_content_neutral_foundation():
    source = (ROOT / "static" / "audiobooks-offline.html").read_text()
    worker = (ROOT / "static" / "sw.js").read_text()
    assert "/static/app.css?v=40" in source
    assert "/static/david-pi-ui.js?v=15" in source
    assert "/static/app.css?v=40" in worker
    assert "/static/david-pi-ui.js?v=15" in worker
    assert "/static/platform.css?v=26" in source
    assert "/static/platform.css?v=26" in worker
    assert "/static/david-pi-ui.css?v=16" in source
    assert "/static/david-pi-ui.css?v=16" in worker
    assert "/static/theme-bootstrap.js?v=8" in source
    assert "/static/theme-bootstrap.js?v=8" in worker
    assert "url.pathname.startsWith('/api/')" in worker
    assert "url.pathname.startsWith('/media/')" in worker


def test_shared_shell_adds_skip_link_and_compact_app_switcher():
    script = (ROOT / "static" / "david-pi-ui.js").read_text()
    styles = (ROOT / "static" / "david-pi-ui.css").read_text()
    assert "Skip to content" in script
    assert "main.setAttribute('tabindex', '-1')" in script
    assert "Open app switcher" in script
    assert "aria-current" in script
    assert ".davidpi-app-button" in styles
    assert ".davidpi-app-switcher a[aria-current" in styles
    assert "[hidden] { display: none !important; }" in styles


def test_recipe_library_actions_wrap_without_widening_mobile_layout():
    styles = (ROOT / "static" / "platform.css").read_text()
    assert ".library-heading { align-items: stretch; flex-direction: column; }" in styles
    assert ".library-heading > div:last-child { flex-wrap: wrap; width: 100%; }" in styles


def test_android_dialog_host_fails_open_to_native_if_shared_asset_is_missing():
    source = (ROOT / "static" / "mobile-dialog-host.js").read_text()
    assert "if (!window.DavidPiModal) return;" in source
