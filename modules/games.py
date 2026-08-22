"""Completed-game rankings and per-person Chess ratings for David-Pi Games."""

import math
import uuid

from flask import jsonify, request

from .identity import current_device
from .platform import PLATFORM_DATA, connect, migrate, utcnow


DB_PATH = PLATFORM_DATA / "platform.db"
GAME_DIFFICULTIES = {
    "sudoku": {"easy", "medium", "hard"},
    "solitaire": {"easy", "medium", "hard"},
    "memory": {"easy", "medium", "hard"},
    "chess": {"easy", "medium", "hard"},
    "checkers": {"easy", "medium", "hard"},
}
GAME_ORDER = ("sudoku", "solitaire", "memory", "chess", "checkers")
DIFFICULTY_FACTOR = {"easy": 1.0, "medium": 1.35, "hard": 1.75}
MOVE_BASELINES = {
    "sudoku": {"easy": 36, "medium": 46, "hard": 54},
    "solitaire": {"easy": 90, "medium": 110, "hard": 130},
    "memory": {"easy": 6, "medium": 8, "hard": 12},
    "checkers": {"easy": 40, "medium": 45, "hard": 50},
}
TARGET_SECONDS = {"sudoku": 600, "solitaire": 900, "memory": 120, "checkers": 600}


def initialize_games(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS game_scores (
            id TEXT PRIMARY KEY,
            game TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            moves INTEGER NOT NULL CHECK (moves > 0),
            duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
            completed_at TEXT NOT NULL,
            completed INTEGER NOT NULL DEFAULT 1 CHECK (completed IN (0, 1))
        )
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(game_scores)")}
    additions = {
        "mode": "TEXT NOT NULL DEFAULT 'solo'",
        "result": "TEXT NOT NULL DEFAULT 'win'",
        "ranking_points": "INTEGER",
        "rating_before": "INTEGER",
        "rating_after": "INTEGER",
        "opponent_rating": "INTEGER",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE game_scores ADD COLUMN {name} {definition}")
    connection.execute(
        """CREATE INDEX IF NOT EXISTS game_scores_board_idx
           ON game_scores(game, completed, ranking_points DESC, duration_ms, completed_at)"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS game_scores_owner_idx
           ON game_scores(owner_id, game, completed, ranking_points DESC, duration_ms)"""
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chess_ratings (
            owner_id TEXT PRIMARY KEY,
            owner_name TEXT NOT NULL,
            rating INTEGER NOT NULL DEFAULT 1000,
            games_played INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """INSERT OR IGNORE INTO chess_ratings
           (owner_id, owner_name, rating, games_played, wins, draws, losses, updated_at)
           SELECT owner_id, MAX(owner_name), 1000, 0, 0, 0, 0, MAX(completed_at)
           FROM game_scores WHERE game='chess' GROUP BY owner_id"""
    )


migrate(DB_PATH, initialize_games)


def _validated_choice(game, difficulty):
    game = str(game or "").strip().casefold()
    difficulty = str(difficulty or "").strip().casefold()
    return game, difficulty, difficulty in GAME_DIFFICULTIES.get(game, set())


def _elapsed_label(duration_ms):
    seconds = max(0, round(int(duration_ms or 0) / 1000))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def _ranking_points(game, difficulty, moves, duration_ms):
    """Comparable points across difficulties; efficiency dominates, time breaks close results."""
    if game == "chess":
        return 0
    baseline = MOVE_BASELINES[game][difficulty]
    factor = DIFFICULTY_FACTOR[difficulty]
    efficiency = min(1.25, baseline / max(1, moves))
    seconds = max(1, duration_ms / 1000)
    speed_bonus = min(250, (TARGET_SECONDS[game] / seconds) * 100)
    return max(1, round((1000 * factor * efficiency) + speed_bonus))


def _serialize(row, rank=None):
    points = row["ranking_points"]
    if points is None and row["game"] != "chess":
        points = _ranking_points(
            row["game"], row["difficulty"], row["moves"], row["duration_ms"]
        )
    result = {
        "id": row["id"],
        "game": row["game"],
        "difficulty": row["difficulty"],
        "player": row["owner_name"],
        "moves": row["moves"],
        "duration_ms": row["duration_ms"],
        "completed_at": row["completed_at"],
        "ranking_points": points,
        "reason": (
            f"{row['moves']} moves · {row['difficulty'].title()} · "
            f"{_elapsed_label(row['duration_ms'])}"
        ),
        "mode": row["mode"],
        "result": row["result"],
        "rating_before": row["rating_before"],
        "rating_after": row["rating_after"],
        "opponent_rating": row["opponent_rating"],
    }
    if rank is not None:
        result["rank"] = rank
    return result


def _rating_payload(row):
    rating = int(row["rating"] if row else 1000)
    return {
        "player": row["owner_name"] if row else None,
        "rating": rating,
        "games_played": int(row["games_played"] if row else 0),
        "wins": int(row["wins"] if row else 0),
        "draws": int(row["draws"] if row else 0),
        "losses": int(row["losses"] if row else 0),
        "opponents": {
            "easy": max(400, rating - 200),
            "medium": rating,
            "hard": min(2800, rating + 200),
        },
    }


def _ensure_chess_rating(connection, actor):
    row = connection.execute(
        "SELECT * FROM chess_ratings WHERE owner_id=?", (actor["owner_id"],)
    ).fetchone()
    if row:
        if row["owner_name"] != actor["name"]:
            connection.execute(
                "UPDATE chess_ratings SET owner_name=? WHERE owner_id=?",
                (actor["name"], actor["owner_id"]),
            )
        return row
    now = utcnow()
    connection.execute(
        """INSERT INTO chess_ratings
           (owner_id, owner_name, rating, games_played, wins, draws, losses, updated_at)
           VALUES (?, ?, 1000, 0, 0, 0, 0, ?)""",
        (actor["owner_id"], actor["name"], now),
    )
    return connection.execute(
        "SELECT * FROM chess_ratings WHERE owner_id=?", (actor["owner_id"],)
    ).fetchone()


def _update_chess_rating(connection, actor, difficulty, result):
    before_row = _ensure_chess_rating(connection, actor)
    before = int(before_row["rating"])
    opponent = {
        "easy": max(400, before - 200),
        "medium": before,
        "hard": min(2800, before + 200),
    }[difficulty]
    actual = {"win": 1.0, "draw": 0.5, "loss": 0.0}[result]
    expected = 1.0 / (1.0 + math.pow(10, (opponent - before) / 400))
    after = max(100, round(before + 32 * (actual - expected)))
    column = {"win": "wins", "draw": "draws", "loss": "losses"}[result]
    connection.execute(
        f"""UPDATE chess_ratings
            SET owner_name=?, rating=?, games_played=games_played+1,
                {column}={column}+1, updated_at=? WHERE owner_id=?""",
        (actor["name"], after, utcnow(), actor["owner_id"]),
    )
    return before, after, opponent


def _ranked_non_chess(connection, game, owner_id=None, limit=10):
    where = "AND owner_id=?" if owner_id else ""
    params = (game, owner_id) if owner_id else (game,)
    rows = connection.execute(
        f"""SELECT * FROM game_scores
            WHERE game=? AND completed=1 AND mode='solo' {where}
            ORDER BY completed_at ASC""",
        params,
    ).fetchall()
    best_by_owner = {}
    for row in rows:
        item = _serialize(row)
        key = row["owner_id"]
        current = best_by_owner.get(key)
        candidate_key = (-item["ranking_points"], item["duration_ms"], item["completed_at"])
        if current is None or candidate_key < current[0]:
            best_by_owner[key] = (candidate_key, item)
    ranked = sorted((value[1] for value in best_by_owner.values()),
                    key=lambda item: (-item["ranking_points"], item["duration_ms"], item["completed_at"]))
    for index, item in enumerate(ranked[:limit], 1):
        item["rank"] = index
    return ranked[:limit]


def _score_overview(connection, actor):
    household = {}
    personal = {}
    for game in GAME_ORDER:
        if game == "chess":
            ratings = connection.execute(
                """SELECT * FROM chess_ratings
                   WHERE games_played > 0 ORDER BY rating DESC, wins DESC, updated_at ASC LIMIT 10"""
            ).fetchall()
            household_rows = []
            for index, row in enumerate(ratings, 1):
                item = _rating_payload(row)
                item.update(rank=index, reason=f"{item['rating']} Elo · {item['games_played']} rated games")
                household_rows.append(item)
            own = connection.execute(
                "SELECT * FROM chess_ratings WHERE owner_id=?", (actor["owner_id"],)
            ).fetchone() if actor["owner_id"] else None
            personal_item = _rating_payload(own) if own and own["games_played"] else None
            if personal_item:
                personal_item["reason"] = (
                    f"{personal_item['rating']} Elo · {personal_item['wins']}W "
                    f"{personal_item['draws']}D {personal_item['losses']}L"
                )
            household[game] = household_rows
            personal[game] = personal_item
        else:
            household[game] = _ranked_non_chess(connection, game)
            own = _ranked_non_chess(connection, game, actor["owner_id"], 1) if actor["owner_id"] else []
            personal[game] = own[0] if own else None
    return household, personal


def init_games(app):
    @app.get("/api/games/chess-rating")
    def chess_rating():
        actor = current_device()
        if not actor["verified"] or not actor["owner_id"]:
            return jsonify(error="A verified Tailscale identity is required."), 403
        with connect(DB_PATH) as connection:
            row = _ensure_chess_rating(connection, actor)
            payload = _rating_payload(row)
            payload["player"] = actor["name"]
        return jsonify(payload)

    @app.post("/api/games/scores")
    def register_game_score():
        actor = current_device()
        if not actor["verified"] or not actor["owner_id"]:
            return jsonify(error="A verified Tailscale identity is required."), 403
        payload = request.get_json(silent=True) or {}
        game, difficulty, valid = _validated_choice(payload.get("game"), payload.get("difficulty"))
        if not valid:
            return jsonify(error="Choose a valid game and difficulty."), 422
        if payload.get("completed") is not True:
            return jsonify(error="Only completed games can be registered."), 422
        mode = str(payload.get("mode", "solo")).strip().casefold()
        if mode != "solo":
            return jsonify(error="Two-player games are not rated."), 422
        result = str(payload.get("result", "win")).strip().casefold()
        if result not in ({"win", "draw", "loss"} if game == "chess" else {"win"}):
            return jsonify(error="Choose a valid completed-game result."), 422
        try:
            moves = int(payload.get("moves"))
            duration_ms = int(payload.get("duration_ms", 0))
        except (TypeError, ValueError):
            return jsonify(error="Moves and elapsed time must be whole numbers."), 422
        if not 1 <= moves <= 1_000_000 or not 0 <= duration_ms <= 30 * 24 * 60 * 60 * 1000:
            return jsonify(error="The completed-game score is outside the allowed range."), 422

        score_id = uuid.uuid4().hex
        completed_at = utcnow()
        points = _ranking_points(game, difficulty, moves, duration_ms)
        rating_before = rating_after = opponent_rating = None
        with connect(DB_PATH) as connection:
            if game == "chess":
                rating_before, rating_after, opponent_rating = _update_chess_rating(
                    connection, actor, difficulty, result
                )
                points = rating_after
            connection.execute(
                """INSERT INTO game_scores
                   (id, game, difficulty, owner_id, owner_name, moves, duration_ms,
                    completed_at, completed, mode, result, ranking_points,
                    rating_before, rating_after, opponent_rating)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                (score_id, game, difficulty, actor["owner_id"], actor["name"],
                 moves, duration_ms, completed_at, mode, result, points,
                 rating_before, rating_after, opponent_rating),
            )
        return jsonify(score={
            "id": score_id, "game": game, "difficulty": difficulty,
            "player": actor["name"], "moves": moves, "duration_ms": duration_ms,
            "completed_at": completed_at, "ranking_points": points,
            "result": result, "rating_before": rating_before,
            "rating_after": rating_after, "opponent_rating": opponent_rating,
        }), 201

    @app.get("/api/games/high-scores")
    def game_high_scores():
        actor = current_device()
        requested_game = request.args.get("game")
        requested_difficulty = request.args.get("difficulty")
        with connect(DB_PATH) as connection:
            if not requested_game and not requested_difficulty:
                household, personal = _score_overview(connection, actor)
                return jsonify(
                    games=list(GAME_ORDER), household=household, personal=personal,
                    current_player=actor["name"],
                    ranking_note=(
                        "Puzzle and board scores combine difficulty, move efficiency, and time. "
                        "Chess is ranked by current Elo."
                    ),
                )

            game, difficulty, valid = _validated_choice(
                requested_game or "sudoku", requested_difficulty or "medium"
            )
            if not valid:
                return jsonify(error="Choose a valid game and difficulty."), 422
            rows = connection.execute(
                """SELECT * FROM game_scores
                   WHERE game=? AND difficulty=? AND completed=1 AND mode='solo'
                   ORDER BY completed_at ASC
                   LIMIT 250""",
                (game, difficulty),
            ).fetchall()

        serialized = [_serialize(row) for row in rows]
        serialized.sort(key=lambda item: (
            -item["ranking_points"], item["duration_ms"], item["completed_at"]
        ))
        owner_by_id = {row["id"]: row["owner_id"] for row in rows}
        household = []
        seen = set()
        personal = None
        for item in serialized:
            owner_id = owner_by_id[item["id"]]
            if owner_id not in seen and len(household) < 10:
                seen.add(owner_id)
                ranked = dict(item)
                ranked["rank"] = len(household) + 1
                household.append(ranked)
            if actor["owner_id"] and owner_id == actor["owner_id"] and personal is None:
                personal = item
        return jsonify(
            game=game, difficulty=difficulty, household=household,
            personal_best=personal, completed_count=len(rows), current_player=actor["name"],
        )
