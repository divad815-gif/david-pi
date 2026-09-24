from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_memory_and_chess_use_mode_specific_semantic_tokens():
    styles = (ROOT / "static" / "games.css").read_text(encoding="utf-8")
    assert ':root[data-theme="dark"] .games-page {' in styles
    for token in (
        "--memory-card-back", "--memory-card-symbol", "--memory-card-face",
        "--memory-card-face-text", "--strategy-board-frame",
        "--strategy-square-light", "--strategy-square-dark",
        "--chess-piece-black", "--chess-piece-white",
        "--chess-piece-white-stroke",
    ):
        assert styles.count(token) >= 3, token

    assert "background: var(--memory-card-back)" in styles
    assert "background: var(--memory-card-face)" in styles
    assert "background: var(--strategy-board-frame)" in styles
    assert "background: var(--strategy-square-light)" in styles
    assert "background: var(--strategy-square-dark)" in styles
    assert "color: var(--chess-piece-black)" in styles
    assert "color: var(--chess-piece-white)" in styles


def test_dark_game_tokens_do_not_reuse_the_inverted_global_ink_as_a_board_surface():
    styles = (ROOT / "static" / "games.css").read_text(encoding="utf-8")
    memory = styles[styles.index(".memory-card {"):styles.index(".board-game-status")]
    strategy = styles[styles.index(".strategy-board {"):styles.index(".chess-controls")]
    assert "background: var(--ink)" not in memory
    assert "background: var(--ink)" not in strategy


def test_games_template_cache_busts_the_theme_patch():
    template = (ROOT / "templates" / "games.html").read_text(encoding="utf-8")
    assert '/static/games.css?v=12' in template


def _luminance(color: str) -> float:
    values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(left: str, right: str) -> float:
    bright, dark = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (bright + 0.05) / (dark + 0.05)


def test_dark_chess_pieces_remain_distinguishable_on_both_square_colors():
    styles = (ROOT / "static" / "games.css").read_text(encoding="utf-8")
    dark_block = styles.split(':root[data-theme="dark"] .games-page {', 1)[1].split("}", 1)[0]
    tokens = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", dark_block))
    for piece in ("chess-piece-black", "chess-piece-white"):
        for square in ("strategy-square-light", "strategy-square-dark"):
            assert _contrast(tokens[piece], tokens[square]) >= 3.0, (piece, square)
