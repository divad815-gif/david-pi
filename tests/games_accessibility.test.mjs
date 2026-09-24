import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const script = readFileSync(new URL('../static/games.js', import.meta.url), 'utf8');
const template = readFileSync(new URL('../templates/games.html', import.meta.url), 'utf8');

test('strategy and number boards expose roving keyboard-grid behavior', () => {
  assert.match(script, /function moveGridFocus\(/);
  assert.match(script, /\['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'\]/);
  assert.match(script, /cell\.tabIndex = index === sudoku\.selected \? 0 : -1/);
  assert.match(script, /aria-selected=/);
  assert.match(script, /aria-invalid/);
  assert.match(script, /chessBoardElement\.addEventListener\('keydown'/);
  assert.match(script, /checkersBoardElement\.addEventListener\('keydown'/);
});

test('board status and dimensions are announced semantically', () => {
  for (const size of ['aria-rowcount="9" aria-colcount="9"', 'aria-rowcount="8" aria-colcount="8"']) {
    assert.ok(template.includes(size));
  }
  for (const id of ['sudokuMessage', 'chessMessage', 'checkersMessage']) {
    assert.match(template, new RegExp(`id="${id}"[^>]+role="status"[^>]+aria-live="polite"`));
  }
});
