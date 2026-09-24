"""Shared weekly plans; recipe originals are never changed by shopping edits."""
import json
import uuid
from datetime import date, timedelta

from flask import jsonify, request
from .identity import require_profile
from .platform import connect, utcnow
from .household_content import authorize, audit_mutation


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS recipe_weekly_plans (
        week TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1,
        plan_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")


def week_key(value):
    day = date.fromisoformat(value)
    return (day - timedelta(days=day.weekday())).isoformat()


def public_plan(connection, row, week):
    plan = json.loads(row['plan_json']) if row else {'recipes': [], 'items': []}
    # Recheck at every read/write: changing a recipe to private must also remove
    # its copied ingredients/title from the shared planner response.
    recipe_ids = [r['id'] for r in plan['recipes']]
    shared = {r['id']: r['title'] for r in connection.execute(
        "SELECT id,title FROM recipes WHERE visibility='shared' AND deleted_at IS NULL AND id IN (" + ','.join('?' for _ in recipe_ids) + ')', recipe_ids)} if recipe_ids else {}
    plan['recipes'] = [{'id': r['id'], 'title': shared[r['id']]} for r in plan['recipes'] if r['id'] in shared]
    ids = {r['id'] for r in plan['recipes']}
    plan['items'] = [i for i in plan['items'] if not i.get('recipe_id') or i['recipe_id'] in ids]
    return dict(plan, week=week, version=row['version'] if row else 0)


def register(bp, db_path, actor_or_error, plain):
    @bp.route('/api/recipes/weekly-plan', methods=['GET', 'POST'])
    @require_profile()
    def weekly_plan():
        actor, _, error = actor_or_error()
        if error:
            return error
        if request.method == 'POST' and not authorize('recipe.weekly_plan.update', actor).allowed:
            return jsonify(error='This account cannot change the household plan.'), 403
        if request.content_length and request.content_length > 131072:
            return jsonify(error='That change is too large.'), 413
        data = request.args
        if request.method == 'POST':
            raw = request.stream.read(131073)
            if len(raw) > 131072:
                return jsonify(error='That change is too large.'), 413
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return jsonify(error='Use a valid plan change.'), 400
        if not isinstance(data, (dict,)) and request.method == 'POST':
            return jsonify(error='Use a valid plan change.'), 400
        if request.method == 'POST' and len(raw) > 16384 and data.get('action') != 'edit_items':
            return jsonify(error='That change is too large.'), 413
        try:
            week = week_key(data.get('week') or date.today().isoformat())
        except (ValueError, TypeError):
            return jsonify(error='Choose a valid week.'), 400
        with connect(db_path) as connection:
            if request.method == 'POST':
                connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM recipe_weekly_plans WHERE week=?', (week,)).fetchone()
            plan = public_plan(connection, row, week)
            if request.method == 'GET':
                if data.get('choices') == '1':
                    query = plain(data.get('q'), 100)
                    choices = connection.execute("SELECT id,title,servings FROM recipes WHERE visibility='shared' AND deleted_at IS NULL AND instr(lower(title),lower(?))>0 ORDER BY title LIMIT 50", (query,)).fetchall()
                    return jsonify(plan=plan, choices=[dict(r) for r in choices])
                return jsonify(plan=plan)
            if type(data.get('version')) is not int or data['version'] != plan['version']:
                return jsonify(error='This week changed on another screen. Review the refreshed list and try again.', conflict=True, plan=plan), 409
            action = data.get('action')
            if action == 'add_recipe':
                recipe = connection.execute("SELECT id,title,ingredients_json FROM recipes WHERE id=? AND visibility='shared' AND deleted_at IS NULL", (str(data.get('recipe_id', ''))[:100],)).fetchone()
                if not recipe:
                    return jsonify(error='Choose a shared recipe from the library.'), 404
                if any(r['id'] == recipe['id'] for r in plan['recipes']):
                    return jsonify(plan=plan)
                lines = json.loads(recipe['ingredients_json'])
                if len(plan['recipes']) >= 35 or len(plan['items']) + len(lines) > 2000:
                    return jsonify(error='This week is full. Remove a recipe or use another week.'), 400
                plan['recipes'].append({'id': recipe['id'], 'title': recipe['title']})
                for line in lines:
                    plan['items'].append({'id': uuid.uuid4().hex, 'recipe_id': recipe['id'], 'text': plain(line, 1000), 'checked': False, 'removed': False})
            elif action == 'remove_recipe':
                rid = data.get('recipe_id')
                plan['recipes'] = [r for r in plan['recipes'] if r['id'] != rid]
                plan['items'] = [i for i in plan['items'] if i.get('recipe_id') != rid]
            elif action == 'add_item':
                text = plain(data.get('text'), 1000)
                if not text or len(plan['items']) >= 2000:
                    return jsonify(error='Enter an item, or shorten this list if it is full.'), 400
                plan['items'].append({'id': uuid.uuid4().hex, 'recipe_id': None, 'text': text, 'checked': False, 'removed': False})
            elif action == 'edit_items':
                ids = data.get('item_ids')
                if not isinstance(ids, list) or not 1 <= len(ids) <= 2000 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
                    return jsonify(error='Choose valid grocery items.'), 400
                fields = {k: data[k] for k in ('checked', 'removed') if k in data}
                if not fields or any(type(v) is not bool for v in fields.values()):
                    return jsonify(error='Use a valid item state.'), 400
                by_id = {i['id']: i for i in plan['items']}
                if any(i not in by_id for i in ids):
                    return jsonify(error='Some items are no longer on this list. Reload the week.'), 409
                for iid in ids:
                    by_id[iid].update(fields)
            elif action == 'edit_item':
                item = next((i for i in plan['items'] if i['id'] == data.get('item_id')), None)
                if item is None:
                    return jsonify(error='That item is no longer on this list.'), 404
                if 'text' in data:
                    text = plain(data['text'], 1000)
                    if not text:
                        return jsonify(error='An item needs a name. Use Remove to exclude it.'), 400
                    item['text'] = text
                for field in ('checked', 'removed'):
                    if field in data:
                        if type(data[field]) is not bool:
                            return jsonify(error='Use a valid item state.'), 400
                        item[field] = data[field]
            else:
                return jsonify(error='Unknown plan change.'), 400
            plan['version'] += 1
            connection.execute('INSERT INTO recipe_weekly_plans VALUES (?,?,?,?) ON CONFLICT(week) DO UPDATE SET version=excluded.version,plan_json=excluded.plan_json,updated_at=excluded.updated_at',
                               (week, plan['version'], json.dumps({'recipes': plan['recipes'], 'items': plan['items']}), utcnow()))
            audit_mutation(connection, actor, domain='recipe_weekly_plan', object_id=week,
                           action=action, before=None, after=None, object_version=plan['version'])
        return jsonify(plan=plan)
