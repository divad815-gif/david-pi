const gameButtons = [...document.querySelectorAll('[data-game]')];
const gamePanels = [...document.querySelectorAll('[data-panel]')];
const gameToast = document.querySelector('#gameToast');
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
const gameStartedAt = {};
const scoreRecorded = {};

function showToast(message) {
  gameToast.textContent = message;
  gameToast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { gameToast.hidden = true; }, 2600);
}

function openGame(name) {
  const scoreButton = document.querySelector('[data-open-scores]');
  scoreButton.classList.remove('selected');
  scoreButton.setAttribute('aria-pressed', 'false');
  if (scoresPanel) scoresPanel.hidden = true;
  gameButtons.forEach((button) => {
    const selected = button.dataset.game === name;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', selected ? 'true' : 'false');
  });
  gamePanels.forEach((panel) => { panel.hidden = panel.dataset.panel !== name; });
  localStorage.setItem('david-pi-game', name);
}
gameButtons.forEach((button) => button.addEventListener('click', () => openGame(button.dataset.game)));

function beginScoredGame(game) {
  gameStartedAt[game] = Date.now();
  scoreRecorded[game] = false;
}

async function registerCompletedGame(game, difficulty, moves, options = {}) {
  if (scoreRecorded[game]) return;
  scoreRecorded[game] = true;
  const duration = Math.max(0, Date.now() - (gameStartedAt[game] || Date.now()));
  try {
    const response = await fetch('/api/games/scores', {
      method: 'POST',
      headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrfToken},
      body: JSON.stringify({
        game, difficulty, moves:Math.max(1, Number(moves) || 1), duration_ms:duration,
        completed:true, mode:options.mode || 'solo', result:options.result || 'win',
      }),
    });
    if (!response.ok) throw new Error('score_not_saved');
    const payload = await response.json();
    if (game === 'chess' && payload.score?.rating_after) {
      chessRating.rating = payload.score.rating_after;
      chessRating.opponents = opponentRatings(chessRating.rating);
      renderChessRating();
      const change = payload.score.rating_after - payload.score.rating_before;
      showToast(`Rated game saved · Elo ${payload.score.rating_after} (${change >= 0 ? '+' : ''}${change})`);
    } else showToast('Complete — high score saved!');
  } catch (_error) {
    scoreRecorded[game] = false;
    showToast('Game complete. The score could not be saved yet.');
  }
}

const scoresPanel = document.querySelector('[data-games-scores]');
const leaderboardList = document.querySelector('#leaderboardList');
let scoreScope = 'household';
let currentScores = null;
const gameNames = {sudoku:'Sudoku', solitaire:'Solitaire', memory:'Memory', chess:'Chess', checkers:'Checkers'};

function formatElapsed(milliseconds) {
  const seconds = Math.max(0, Math.round(Number(milliseconds || 0) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

function formatDifficulty(value) {
  const difficulty = String(value || '').toLowerCase();
  return difficulty ? difficulty[0].toUpperCase() + difficulty.slice(1) : 'Unknown';
}

function escapeScoreText(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;',
  })[character]);
}

function renderHighScores() {
  if (!currentScores) return;
  leaderboardList.innerHTML = currentScores.games.map((game) => {
    const row = scoreScope === 'personal'
      ? currentScores.personal[game]
      : (currentScores.household[game] || [])[0];
    const empty = scoreScope === 'personal' ? 'No personal score yet' : 'No completed games yet';
    const value = game === 'chess' && row ? `${row.rating} Elo`
      : row ? `${row.moves} moves · ${formatDifficulty(row.difficulty)}` : empty;
    const explanation = row ? escapeScoreText(row.reason) : 'Complete a rated game to appear here.';
    const player = row ? escapeScoreText(row.player || currentScores.current_player) : 'Waiting for a result';
    const points = row && game !== 'chess' ? `<span>${row.ranking_points} pts</span>` : '';
    return `<article class="score-summary-card" data-score-game="${game}">
      <div class="score-game-icon">${game === 'sudoku' ? '⌗' : game === 'solitaire' ? '♠' : game === 'memory' ? '✦' : game === 'chess' ? '♞' : '●'}</div>
      <div class="score-summary-copy"><p>${gameNames[game]}</p><h3>${player}</h3><strong>${value}</strong><small>${explanation}</small></div>${points}
    </article>`;
  }).join('');
}

async function loadHighScores() {
  leaderboardList.innerHTML = '<p class="leaderboard-empty">Loading completed games…</p>';
  try {
    const response = await fetch('/api/games/high-scores', {cache:'no-store'});
    if (!response.ok) throw new Error('scores_unavailable');
    currentScores = await response.json();
    renderHighScores();
  } catch (_error) {
    currentScores = null;
    leaderboardList.innerHTML = '<p class="leaderboard-empty">High scores are temporarily unavailable.</p>';
  }
}

function openScores() {
  gameButtons.forEach((button) => { button.classList.remove('selected'); button.setAttribute('aria-pressed', 'false'); });
  const scoreButton = document.querySelector('[data-open-scores]');
  scoreButton.classList.add('selected');
  scoreButton.setAttribute('aria-pressed', 'true');
  gamePanels.forEach((panel) => { panel.hidden = true; });
  scoresPanel.hidden = false;
  loadHighScores();
}
document.querySelector('[data-open-scores]').addEventListener('click', openScores);
document.querySelectorAll('[data-score-scope]').forEach((button) => button.addEventListener('click', () => {
  scoreScope = button.dataset.scoreScope;
  document.querySelectorAll('[data-score-scope]').forEach((item) => {
    const selected = item === button;
    item.classList.toggle('selected', selected);
    item.setAttribute('aria-pressed', selected ? 'true' : 'false');
  });
  renderHighScores();
}));

function moveGridFocus(event, board, columns) {
  if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return null;
  const cells = [...board.querySelectorAll('[role="gridcell"]')];
  const current = Math.max(0, cells.indexOf(event.target.closest?.('[role="gridcell"]')));
  const rowStart = Math.floor(current / columns) * columns;
  let next = current;
  if (event.key === 'ArrowLeft') next = Math.max(rowStart, current - 1);
  if (event.key === 'ArrowRight') next = Math.min(rowStart + columns - 1, current + 1);
  if (event.key === 'ArrowUp') next = Math.max(0, current - columns);
  if (event.key === 'ArrowDown') next = Math.min(cells.length - 1, current + columns);
  if (event.key === 'Home') next = rowStart;
  if (event.key === 'End') next = Math.min(cells.length - 1, rowStart + columns - 1);
  event.preventDefault();
  cells.forEach((cell, index) => { cell.tabIndex = index === next ? 0 : -1; });
  cells[next]?.focus({preventScroll:true});
  return cells[next] ? Number(cells[next].dataset.index) : null;
}

function restoreGridFocus(board, index, shouldFocus) {
  if (!shouldFocus) return;
  const cell = board.querySelector(`[data-index="${index}"]`);
  if (cell) requestAnimationFrame(() => cell.focus({preventScroll:true}));
}

// Sudoku
const baseSolution = '534678912672195348198342567859761423426853791713924856961537284287419635345286179';
const sudokuBoard = document.querySelector('#sudokuBoard');
const sudokuPad = document.querySelector('#sudokuPad');
const sudokuMessage = document.querySelector('#sudokuMessage');
let sudoku = { solution: '', puzzle: [], values: [], selected: -1, wrong: new Set(), moves: 0 };

function shuffled(values) {
  const copy = [...values];
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const other = Math.floor(Math.random() * (index + 1));
    [copy[index], copy[other]] = [copy[other], copy[index]];
  }
  return copy;
}

function newSudoku() {
  const difficulty = document.querySelector('#sudokuDifficulty').value;
  const blanks = {easy: 36, medium: 46, hard: 54}[difficulty];
  const mapping = shuffled(['1','2','3','4','5','6','7','8','9']);
  sudoku.solution = [...baseSolution].map((digit) => mapping[Number(digit) - 1]).join('');
  sudoku.puzzle = [...sudoku.solution];
  shuffled([...Array(81).keys()]).slice(0, blanks).forEach((index) => { sudoku.puzzle[index] = ''; });
  sudoku.values = [...sudoku.puzzle];
  sudoku.selected = sudoku.puzzle.findIndex((value) => !value);
  sudoku.wrong = new Set();
  sudoku.moves = 0;
  beginScoredGame('sudoku');
  sudokuMessage.textContent = 'Choose an empty square.';
  renderSudoku();
}

function renderSudoku(forceFocus = false) {
  const hadFocus = forceFocus || sudokuBoard.contains(document.activeElement);
  const selectedRow = Math.floor(sudoku.selected / 9);
  const selectedColumn = sudoku.selected % 9;
  const selectedValue = sudoku.values[sudoku.selected];
  sudokuBoard.innerHTML = '';
  sudoku.values.forEach((value, index) => {
    const cell = document.createElement('button');
    cell.type = 'button';
    cell.className = 'sudoku-cell';
    cell.dataset.index = index;
    cell.setAttribute('role', 'gridcell');
    cell.setAttribute('aria-rowindex', String(Math.floor(index / 9) + 1));
    cell.setAttribute('aria-colindex', String((index % 9) + 1));
    cell.setAttribute('aria-label', `Row ${Math.floor(index / 9) + 1}, column ${(index % 9) + 1}${value ? `, ${value}` : ', empty'}`);
    cell.tabIndex = index === sudoku.selected ? 0 : -1;
    cell.textContent = value;
    if (sudoku.puzzle[index]) cell.classList.add('fixed');
    if (Math.floor(index / 9) === selectedRow || index % 9 === selectedColumn) cell.classList.add('related');
    if (selectedValue && value === selectedValue) cell.classList.add('same');
    if (index === sudoku.selected) { cell.classList.add('selected'); cell.setAttribute('aria-selected', 'true'); }
    else cell.setAttribute('aria-selected', 'false');
    if (sudoku.wrong.has(index)) { cell.classList.add('wrong'); cell.setAttribute('aria-invalid', 'true'); }
    sudokuBoard.append(cell);
  });
  restoreGridFocus(sudokuBoard, sudoku.selected, hadFocus);
}

function enterSudoku(value) {
  if (sudoku.selected < 0 || sudoku.puzzle[sudoku.selected]) return;
  if (sudoku.values[sudoku.selected] === value) return;
  sudoku.values[sudoku.selected] = value;
  sudoku.moves += 1;
  sudoku.wrong.delete(sudoku.selected);
  renderSudoku();
  if (sudoku.values.join('') === sudoku.solution) {
    sudokuMessage.textContent = 'Perfect — puzzle complete!';
    registerCompletedGame('sudoku', document.querySelector('#sudokuDifficulty').value, sudoku.moves);
  }
}

sudokuBoard.addEventListener('click', (event) => {
  const cell = event.target.closest('.sudoku-cell');
  if (!cell) return;
  sudoku.selected = Number(cell.dataset.index);
  renderSudoku(true);
});
sudokuBoard.addEventListener('keydown', (event) => {
  const index = moveGridFocus(event, sudokuBoard, 9);
  if (index === null) return;
  sudoku.selected = index;
  renderSudoku(true);
});
for (let number = 1; number <= 9; number += 1) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = number;
  button.addEventListener('click', () => enterSudoku(String(number)));
  sudokuPad.append(button);
}
document.querySelector('#clearSudoku').addEventListener('click', () => enterSudoku(''));
document.querySelector('#newSudoku').addEventListener('click', newSudoku);
document.querySelector('#sudokuDifficulty').addEventListener('change', newSudoku);
document.querySelector('#checkSudoku').addEventListener('click', () => {
  sudoku.wrong = new Set();
  sudoku.values.forEach((value, index) => {
    if (value && value !== sudoku.solution[index]) sudoku.wrong.add(index);
  });
  if (sudoku.wrong.size) sudokuMessage.textContent = `${sudoku.wrong.size} square${sudoku.wrong.size === 1 ? ' needs' : 's need'} another look.`;
  else if (sudoku.values.some((value) => !value)) sudokuMessage.textContent = 'Everything entered so far looks good.';
  else {
    sudokuMessage.textContent = 'Perfect — puzzle complete!';
    registerCompletedGame('sudoku', document.querySelector('#sudokuDifficulty').value, sudoku.moves);
  }
  renderSudoku();
});
document.addEventListener('keydown', (event) => {
  if (document.querySelector('#sudokuGame').hidden) return;
  if (/^[1-9]$/.test(event.key)) enterSudoku(event.key);
  if (event.key === 'Backspace' || event.key === 'Delete') enterSudoku('');
});

// Klondike Solitaire
const suits = [
  {symbol: '♠', color: 'black'}, {symbol: '♥', color: 'red'},
  {symbol: '♦', color: 'red'}, {symbol: '♣', color: 'black'},
];
const rankName = (rank) => ({1:'A',11:'J',12:'Q',13:'K'}[rank] || String(rank));
let solitaire;
let solitaireUndo = [];
let solitaireSelection = null;
const solitaireTop = document.querySelector('#solitaireTop');
const tableauElement = document.querySelector('#tableau');
const solitaireMessage = document.querySelector('#solitaireMessage');

function newSolitaire() {
  const difficulty = document.querySelector('#solitaireDifficulty').value;
  const deck = shuffled(suits.flatMap((suit) => [...Array(13)].map((_, index) => ({
    id: `${suit.symbol}${index + 1}-${Math.random()}`, suit: suit.symbol,
    color: suit.color, rank: index + 1, faceUp: false,
  }))));
  const tableau = [...Array(7)].map(() => []);
  for (let column = 0; column < 7; column += 1) {
    for (let row = 0; row <= column; row += 1) tableau[column].push(deck.pop());
    tableau[column][tableau[column].length - 1].faceUp = true;
  }
  solitaire = {stock: deck, waste: [], foundations: suits.map(() => []), tableau, moves: 0, difficulty, redeals:0};
  solitaireUndo = [];
  solitaireSelection = null;
  solitaireMessage.textContent = 'Tap a card, then tap where it should go.';
  beginScoredGame('solitaire');
  renderSolitaire();
}

function saveSolitaireUndo() {
  solitaireUndo.push(JSON.stringify(solitaire));
  if (solitaireUndo.length > 30) solitaireUndo.shift();
}

function isSelectedSolitaireCard(pile, column = '', index = '') {
  if (!solitaireSelection || solitaireSelection.pile !== pile) return false;
  if (pile === 'waste') return true;
  if (pile === 'foundation') return Number(solitaireSelection.column) === Number(column);
  return Number(solitaireSelection.column) === Number(column)
    && Number(solitaireSelection.index) === Number(index);
}

function cardButton(card, pile, column = '', index = '') {
  const selected = isSelectedSolitaireCard(pile, column, index);
  return `<button type="button" class="playing-card ${card.faceUp ? card.color : 'face-down'} ${selected ? 'selected' : ''}"
    data-pile="${pile}" data-column="${column}" data-index="${index}" aria-label="${card.faceUp ? `${rankName(card.rank)} of ${card.suit}` : 'Face-down card'}">
    <b>${rankName(card.rank)}</b><span>${card.suit}</span></button>`;
}

function renderSolitaire() {
  const stock = solitaire.stock.length
    ? `<button type="button" class="playing-card face-down" data-action="draw" aria-label="Draw from stock"><b>•</b><span>•</span></button>`
    : `<button type="button" class="card-slot" data-action="draw" aria-label="Recycle cards">↻</button>`;
  const waste = solitaire.waste.length
    ? cardButton(solitaire.waste.at(-1), 'waste')
    : '<div class="card-slot" aria-label="Empty waste pile"></div>';
  const foundations = solitaire.foundations.map((pile, column) => pile.length
    ? cardButton(pile.at(-1), 'foundation', column, pile.length - 1)
    : `<button type="button" class="card-slot" data-pile="foundation" data-column="${column}" aria-label="Empty ${suits[column].symbol} foundation">${suits[column].symbol}</button>`
  ).join('');
  solitaireTop.innerHTML = `${stock}${waste}<div></div>${foundations}`;
  tableauElement.innerHTML = solitaire.tableau.map((column, columnIndex) => `
    <div class="tableau-column" data-pile="tableau" data-column="${columnIndex}">
      ${column.length ? column.map((card, index) => cardButton(card, 'tableau', columnIndex, index)).join('')
        : `<button type="button" class="card-slot" data-pile="tableau" data-column="${columnIndex}" aria-label="Empty tableau column">K</button>`}
    </div>`).join('');
  document.querySelector('#solitaireMoves').textContent = solitaire.moves;
  document.querySelector('#undoSolitaire').disabled = !solitaireUndo.length;
  if (solitaire.foundations.reduce((total, pile) => total + pile.length, 0) === 52) {
    solitaireMessage.textContent = 'You won!';
    registerCompletedGame('solitaire', solitaire.difficulty, solitaire.moves);
  }
}

function selectedCards() {
  if (!solitaireSelection) return [];
  if (solitaireSelection.pile === 'waste') return [solitaire.waste.at(-1)];
  if (solitaireSelection.pile === 'foundation') return [solitaire.foundations[solitaireSelection.column].at(-1)];
  return solitaire.tableau[solitaireSelection.column].slice(solitaireSelection.index);
}

function validTableauSequence(cards) {
  return cards.every((card, index) => !index || (
    cards[index - 1].rank === card.rank + 1 && cards[index - 1].color !== card.color
  ));
}

function canPlaceOnTableau(card, column) {
  const target = solitaire.tableau[column].at(-1);
  return target ? target.faceUp && target.rank === card.rank + 1 && target.color !== card.color : card.rank === 13;
}

function removeSelection() {
  let cards;
  if (solitaireSelection.pile === 'waste') cards = [solitaire.waste.pop()];
  else if (solitaireSelection.pile === 'foundation') cards = [solitaire.foundations[solitaireSelection.column].pop()];
  else {
    const source = solitaire.tableau[solitaireSelection.column];
    cards = source.splice(solitaireSelection.index);
    if (source.length && !source.at(-1).faceUp) source.at(-1).faceUp = true;
  }
  return cards;
}

function moveToTableau(column) {
  const cards = selectedCards();
  if (!cards.length || !validTableauSequence(cards) || !canPlaceOnTableau(cards[0], column)) return false;
  if (solitaireSelection.pile === 'tableau' && solitaireSelection.column === column) return false;
  saveSolitaireUndo();
  solitaire.tableau[column].push(...removeSelection());
  solitaire.moves += 1;
  solitaireSelection = null;
  return true;
}

function moveToFoundation(column) {
  const cards = selectedCards();
  if (cards.length !== 1) return false;
  const card = cards[0];
  const expectedSuit = suits[column].symbol;
  const target = solitaire.foundations[column].at(-1);
  if (card.suit !== expectedSuit || card.rank !== (target ? target.rank + 1 : 1)) return false;
  saveSolitaireUndo();
  solitaire.foundations[column].push(removeSelection()[0]);
  solitaire.moves += 1;
  solitaireSelection = null;
  return true;
}

function selectCard(pile, column, index) {
  if (pile === 'waste') {
    if (!solitaire.waste.length) return;
    solitaireSelection = {pile};
  } else if (pile === 'foundation') {
    if (!solitaire.foundations[column].length) return;
    solitaireSelection = {pile, column};
  } else {
    const card = solitaire.tableau[column][index];
    if (!card?.faceUp) {
      if (index === solitaire.tableau[column].length - 1) {
        saveSolitaireUndo();
        card.faceUp = true;
        solitaire.moves += 1;
      }
      return;
    }
    const cards = solitaire.tableau[column].slice(index);
    if (validTableauSequence(cards)) solitaireSelection = {pile, column, index};
  }
}

function handleSolitaireClick(event) {
  const target = event.target.closest('[data-action],[data-pile]');
  if (!target) return;
  if (target.dataset.action === 'draw') {
    saveSolitaireUndo();
    solitaireSelection = null;
    if (solitaire.stock.length) {
      const drawCount = solitaire.difficulty === 'easy' ? 1 : 3;
      for (let drawn = 0; drawn < drawCount && solitaire.stock.length; drawn += 1) {
        const card = solitaire.stock.pop(); card.faceUp = true; solitaire.waste.push(card);
      }
    } else if (solitaire.waste.length) {
      if (solitaire.difficulty === 'hard' && solitaire.redeals >= 1) {
        solitaireUndo.pop();
        solitaireMessage.textContent = 'Hard mode allows one pass through the deck.';
        renderSolitaire();
        return;
      }
      solitaire.stock = solitaire.waste.reverse().map((card) => ({...card, faceUp: false}));
      solitaire.waste = [];
      solitaire.redeals += 1;
    } else {
      solitaireUndo.pop();
      return;
    }
    solitaire.moves += 1;
    renderSolitaire();
    return;
  }
  const pile = target.dataset.pile;
  const column = Number(target.dataset.column);
  const index = Number(target.dataset.index);
  if (solitaireSelection) {
    const moved = pile === 'foundation' ? moveToFoundation(column)
      : pile === 'tableau' ? moveToTableau(column) : false;
    if (!moved) {
      if (isSelectedSolitaireCard(pile, column, index)) solitaireSelection = null;
      else selectCard(pile, column, index);
    }
  } else selectCard(pile, column, index);
  renderSolitaire();
}
solitaireTop.addEventListener('click', handleSolitaireClick);
tableauElement.addEventListener('click', handleSolitaireClick);
document.querySelector('#newSolitaire').addEventListener('click', newSolitaire);
document.querySelector('#solitaireDifficulty').addEventListener('change', newSolitaire);
document.querySelector('#undoSolitaire').addEventListener('click', () => {
  if (!solitaireUndo.length) return;
  solitaire = JSON.parse(solitaireUndo.pop());
  solitaireSelection = null;
  solitaireMessage.textContent = 'Last move undone.';
  renderSolitaire();
});

// Memory Match
const memorySymbols = ['☀','☂','★','♥','♣','♪','◆','●','☕','⚓','✿','☾'];
let memory;
function newMemory() {
  const difficulty = document.querySelector('#memoryDifficulty').value;
  const pairCount = {easy:6, medium:8, hard:12}[difficulty];
  const symbols = memorySymbols.slice(0, pairCount);
  memory = {
    cards: shuffled([...symbols, ...symbols]).map((symbol, index) => ({id: index, symbol, revealed: false, matched: false})),
    open: [], moves: 0, locked: false, difficulty, pairCount,
  };
  document.querySelector('#memoryBoard').dataset.pairs = String(pairCount);
  document.querySelector('#memoryMoves').textContent = '0';
  document.querySelector('#memoryMessage').textContent = `Find all ${pairCount} matching pairs.`;
  beginScoredGame('memory');
  renderMemory();
}
function renderMemory() {
  document.querySelector('#memoryBoard').innerHTML = memory.cards.map((card, index) => `
    <button type="button" class="memory-card ${card.revealed ? 'revealed' : ''} ${card.matched ? 'matched' : ''}"
      data-index="${index}" aria-label="${card.revealed || card.matched ? card.symbol : 'Hidden card'}"
      ${card.matched ? 'disabled' : ''}><span aria-hidden="true">${card.symbol}</span></button>`).join('');
}
document.querySelector('#memoryBoard').addEventListener('click', (event) => {
  const button = event.target.closest('.memory-card');
  if (!button || memory.locked) return;
  const index = Number(button.dataset.index);
  const card = memory.cards[index];
  if (card.revealed || card.matched) return;
  card.revealed = true;
  memory.open.push(index);
  renderMemory();
  if (memory.open.length < 2) return;
  memory.moves += 1;
  document.querySelector('#memoryMoves').textContent = memory.moves;
  const [first, second] = memory.open.map((position) => memory.cards[position]);
  if (first.symbol === second.symbol) {
    first.matched = second.matched = true;
    first.revealed = second.revealed = false;
    memory.open = [];
    renderMemory();
    if (memory.cards.every((item) => item.matched)) {
      document.querySelector('#memoryMessage').textContent = `All matched in ${memory.moves} moves!`;
      registerCompletedGame('memory', memory.difficulty, memory.moves);
    }
  } else {
    memory.locked = true;
    setTimeout(() => {
      first.revealed = second.revealed = false;
      memory.open = [];
      memory.locked = false;
      renderMemory();
    }, 750);
  }
});
document.querySelector('#newMemory').addEventListener('click', newMemory);
document.querySelector('#memoryDifficulty').addEventListener('change', newMemory);

// Chess
const ChessEngine = window.ChessEngine;
const chessGlyphs = {
  white: {king:'♚', queen:'♛', rook:'♜', bishop:'♝', knight:'♞', pawn:'♟'},
  black: {king:'♚', queen:'♛', rook:'♜', bishop:'♝', knight:'♞', pawn:'♟'},
};
const chessBoardElement = document.querySelector('#chessBoard');
const chessMessage = document.querySelector('#chessMessage');
let chess;
let chessUndo = [];
let chessAiThinking = false;
let chessFocus = 0;
let chessRating = {rating:1000, games_played:0, wins:0, draws:0, losses:0, opponents:{easy:800, medium:1000, hard:1200}};
const chessAt = ChessEngine.at;
const chessInside = ChessEngine.inside;
const otherChessColor = ChessEngine.other;
const chessSideSelect = document.querySelector('#chessSide');
const savedChessSide = localStorage.getItem('david-pi-chess-side');
if (['alternate', 'white', 'black'].includes(savedChessSide)) chessSideSelect.value = savedChessSide;

function opponentRatings(rating) {
  return {easy:Math.max(400, rating - 200), medium:rating, hard:Math.min(2800, rating + 200)};
}

function renderChessRating() {
  const summary = document.querySelector('#chessRatingSummary');
  if (document.querySelector('#chessMode').value === 'two') {
    summary.textContent = 'Two-player games are local pass-and-play and are not Elo rated.';
    return;
  }
  const difficulty = document.querySelector('#chessDifficulty').value;
  const opponent = chessRating.opponents[difficulty];
  const side = chess?.playerColor ? ` · Playing as ${formatDifficulty(chess.playerColor)}` : '';
  summary.textContent = `Your Elo: ${chessRating.rating} · ${formatDifficulty(difficulty)} opponent: ${opponent} Elo${side}`;
}

async function loadChessRating() {
  try {
    const response = await fetch('/api/games/chess-rating', {cache:'no-store'});
    if (!response.ok) throw new Error('rating_unavailable');
    chessRating = await response.json();
  } catch (_error) {
    chessRating = {rating:1000, games_played:0, wins:0, draws:0, losses:0, opponents:opponentRatings(1000)};
  }
  renderChessRating();
}

function syncBoardModeControls(game) {
  const mode = document.querySelector(`#${game}Mode`).value;
  document.querySelector(`#${game}DifficultyField`).hidden = mode === 'two';
  if (game === 'chess') document.querySelector('#chessSideField').hidden = mode === 'two';
  if (game === 'chess') renderChessRating();
}

function resolveChessPlayerColor() {
  const preference = chessSideSelect.value;
  if (preference === 'white' || preference === 'black') return preference;
  const next = localStorage.getItem('david-pi-chess-alternate-next') === 'black' ? 'black' : 'white';
  localStorage.setItem('david-pi-chess-alternate-next', otherChessColor(next));
  return next;
}

function newChess() {
  syncBoardModeControls('chess');
  const solo = document.querySelector('#chessMode').value === 'solo';
  const playerColor = solo ? resolveChessPlayerColor() : null;
  chess = {
    ...ChessEngine.initialState(), selected:null, legal:[], lastMove:null, over:false, moves:0,
    difficulty:document.querySelector('#chessDifficulty').value,
    playerColor,
    aiColor:solo ? otherChessColor(playerColor) : null,
  };
  chessUndo = [];
  chessAiThinking = false;
  chessMessage.textContent = solo ? `You are ${formatDifficulty(playerColor)}. White moves first.` : 'White moves first.';
  beginScoredGame('chess');
  renderChess();
  renderChessRating();
  if (solo && chess.aiColor === 'white') maybeChessAi();
}

function chessPseudoMoves(board, from, attacksOnly = false) {
  const piece = board[from];
  if (!piece) return [];
  const row = Math.floor(from / 8);
  const column = from % 8;
  const moves = [];
  const addStep = (targetRow, targetColumn, captureOnly = false) => {
    if (!chessInside(targetRow, targetColumn)) return;
    const target = board[chessAt(targetRow, targetColumn)];
    if ((captureOnly && target && target.color !== piece.color)
        || (!captureOnly && (!target || target.color !== piece.color))) {
      moves.push(chessAt(targetRow, targetColumn));
    }
  };
  const slide = (directions) => directions.forEach(([rowStep, columnStep]) => {
    let targetRow = row + rowStep;
    let targetColumn = column + columnStep;
    while (chessInside(targetRow, targetColumn)) {
      const targetIndex = chessAt(targetRow, targetColumn);
      const target = board[targetIndex];
      if (!target) moves.push(targetIndex);
      else {
        if (target.color !== piece.color) moves.push(targetIndex);
        break;
      }
      targetRow += rowStep;
      targetColumn += columnStep;
    }
  });
  if (piece.type === 'pawn') {
    const direction = piece.color === 'white' ? -1 : 1;
    [-1, 1].forEach((step) => {
      const targetRow = row + direction;
      const targetColumn = column + step;
      if (!chessInside(targetRow, targetColumn)) return;
      const targetIndex = chessAt(targetRow, targetColumn);
      if (attacksOnly || (board[targetIndex] && board[targetIndex].color !== piece.color)) moves.push(targetIndex);
    });
    if (!attacksOnly) {
      const one = chessAt(row + direction, column);
      if (chessInside(row + direction, column) && !board[one]) {
        moves.push(one);
        const startRow = piece.color === 'white' ? 6 : 1;
        const two = chessAt(row + direction * 2, column);
        if (row === startRow && !board[two]) moves.push(two);
      }
    }
  } else if (piece.type === 'knight') {
    [[-2,-1],[-2,1],[-1,-2],[-1,2],[1,-2],[1,2],[2,-1],[2,1]]
      .forEach(([a,b]) => addStep(row + a, column + b));
  } else if (piece.type === 'bishop') slide([[-1,-1],[-1,1],[1,-1],[1,1]]);
  else if (piece.type === 'rook') slide([[-1,0],[1,0],[0,-1],[0,1]]);
  else if (piece.type === 'queen') slide([[-1,-1],[-1,1],[1,-1],[1,1],[-1,0],[1,0],[0,-1],[0,1]]);
  else if (piece.type === 'king') {
    for (let a = -1; a <= 1; a += 1) for (let b = -1; b <= 1; b += 1) {
      if (a || b) addStep(row + a, column + b);
    }
  }
  return moves;
}

function chessInCheck(board, color) {
  const king = board.findIndex((piece) => piece?.type === 'king' && piece.color === color);
  if (king < 0) return true;
  return board.some((piece, index) => piece && piece.color !== color
    && chessPseudoMoves(board, index, true).includes(king));
}

function chessLegalMoves(board, from) {
  const piece = board[from];
  if (!piece) return [];
  return chessPseudoMoves(board, from).filter((to) => {
    const next = board.map((item) => item ? {...item} : null);
    next[to] = next[from];
    next[from] = null;
    return !chessInCheck(next, piece.color);
  });
}

function chessHasMove(color) {
  return ChessEngine.allMoves(chess, color).length > 0;
}

function allChessMoves(board, color) {
  return board.flatMap((piece, from) => piece?.color === color
    ? chessLegalMoves(board, from).map((to) => ({from, to})) : []);
}

function chessBoardAfter(board, move) {
  const next = board.map((piece) => piece ? {...piece} : null);
  next[move.to] = next[move.from];
  next[move.from] = null;
  const row = Math.floor(move.to / 8);
  if (next[move.to]?.type === 'pawn' && (row === 0 || row === 7)) next[move.to].type = 'queen';
  return next;
}

function chessScore(board, color) {
  const values = {pawn:100, knight:320, bishop:330, rook:500, queen:900, king:20000};
  return board.reduce((score, piece) => score + (piece ? values[piece.type] * (piece.color === color ? 1 : -1) : 0), 0);
}

function chooseChessAiMove() {
  const aiColor = chess.aiColor;
  const moves = ChessEngine.allMoves(chess, aiColor);
  if (!moves.length) return null;
  const aiRating = chessRating.opponents[chess.difficulty] || 1000;
  const useReplySearch = chess.difficulty === 'hard' || aiRating >= 1250;
  const noise = Math.max(2, 32 - ((aiRating - 400) / 35));
  let best = -Infinity;
  let choices = [];
  moves.forEach((move) => {
    const next = ChessEngine.applyMove(chess, move);
    const center = 8 - (Math.abs(3.5 - (move.to % 8)) + Math.abs(3.5 - Math.floor(move.to / 8)));
    let score = chessScore(next.board, aiColor) + center * 2;
    if (useReplySearch) {
      const replies = ChessEngine.allMoves(next, chess.playerColor);
      score = replies.length
        ? Math.min(...replies.map((reply) => chessScore(ChessEngine.applyMove(next, reply).board, aiColor))) + center
        : chessScore(next.board, aiColor) + (ChessEngine.inCheck(next, chess.playerColor) ? 100000 : 0);
    }
    score += Math.random() * noise;
    if (score > best + 0.01) { best = score; choices = [move]; }
    else if (Math.abs(score - best) < 0.01) choices.push(move);
  });
  return choices[Math.floor(Math.random() * choices.length)];
}

function maybeChessAi() {
  if (document.querySelector('#chessMode').value !== 'solo' || chess.turn !== chess.aiColor || chess.over || chessAiThinking) return;
  chessAiThinking = true;
  chessMessage.textContent = `${window.davidPiServerName || "Home server"} (${formatDifficulty(chess.aiColor)}) is thinking…`;
  setTimeout(() => {
    const move = chooseChessAiMove();
    chessAiThinking = false;
    if (move && !chess.over && chess.turn === chess.aiColor) moveChess(move.from, move.to);
  }, 360);
}

function renderChess(forceFocus = false) {
  const focusedSquare = chessBoardElement.contains(document.activeElement)
    ? Number(document.activeElement.closest('[data-index]')?.dataset.index) : null;
  if (focusedSquare !== null && Number.isInteger(focusedSquare)) chessFocus = focusedSquare;
  const hadFocus = forceFocus || focusedSquare !== null;
  const indices = Array.from({length:64}, (_, index) => index);
  if (document.querySelector('#chessMode').value === 'solo' && chess.playerColor === 'black') indices.reverse();
  chessBoardElement.innerHTML = indices.map((index) => {
    const piece = chess.board[index];
    const row = Math.floor(index / 8);
    const legal = chess.legal.includes(index);
    const classes = ['strategy-square', (row + index % 8) % 2 ? 'dark' : ''];
    if (index === chess.selected) classes.push('selected');
    if (legal) classes.push(piece ? 'capture' : 'legal');
    if (chess.lastMove?.includes(index)) classes.push('last-move');
    const state = index === chess.selected ? ', selected' : legal ? (piece ? ', available capture' : ', available move') : '';
    const label = piece ? `${piece.color} ${piece.type}${state}` : `Empty square ${String.fromCharCode(97 + index % 8)}${8 - row}${state}`;
    return `<button type="button" class="${classes.join(' ')}" data-index="${index}" role="gridcell" aria-rowindex="${row + 1}" aria-colindex="${index % 8 + 1}" aria-selected="${index === chess.selected}" tabindex="${index === chessFocus ? '0' : '-1'}" aria-label="${label}">
      ${piece ? `<span class="chess-piece ${piece.color}">${chessGlyphs[piece.color][piece.type]}</span>` : ''}</button>`;
  }).join('');
  document.querySelector('#undoChess').disabled = !chessUndo.length;
  document.querySelector('#chessMoves').textContent = chess.moves;
  restoreGridFocus(chessBoardElement, chessFocus, hadFocus);
}

function moveChess(from, to) {
  chessUndo.push(JSON.stringify(chess));
  chessFocus = to;
  const movedColor = chess.turn;
  const next = ChessEngine.applyMove(chess, {from, to});
  chess = {...next, selected:null, legal:[], lastMove:[from, to], over:false, moves:chess.moves + 1};
  const inCheck = ChessEngine.inCheck(chess, chess.turn);
  if (!chessHasMove(chess.turn)) {
    chess.over = true;
    chessMessage.textContent = inCheck
      ? `Checkmate — ${otherChessColor(chess.turn)[0].toUpperCase() + otherChessColor(chess.turn).slice(1)} wins!`
      : 'Draw — no legal moves.';
    showToast(chessMessage.textContent);
    const winner = inCheck ? otherChessColor(chess.turn) : null;
    if (document.querySelector('#chessMode').value === 'solo') {
      const result = winner === chess.playerColor ? 'win' : winner === chess.aiColor ? 'loss' : 'draw';
      registerCompletedGame('chess', chess.difficulty, chess.moves, {mode:'solo', result});
    }
  } else {
    const name = chess.turn[0].toUpperCase() + chess.turn.slice(1);
    const special = next.special?.startsWith('castle') ? `${formatDifficulty(movedColor)} castled. `
      : next.special === 'en-passant' ? `${formatDifficulty(movedColor)} captured en passant. ` : '';
    chessMessage.textContent = `${special}${name} to move${inCheck ? ' — check!' : '.'}`;
  }
  renderChess(true);
  maybeChessAi();
}

chessBoardElement.addEventListener('click', (event) => {
  if (chess.over || chessAiThinking || (document.querySelector('#chessMode').value === 'solo' && chess.turn === chess.aiColor)) return;
  const square = event.target.closest('.strategy-square');
  if (!square) return;
  const index = Number(square.dataset.index);
  chessFocus = index;
  const piece = chess.board[index];
  if (chess.selected !== null && chess.legal.includes(index)) {
    moveChess(chess.selected, index);
    return;
  }
  if (piece?.color === chess.turn) {
    chess.selected = index;
    chess.legal = ChessEngine.legalMoves(chess, index);
  } else {
    chess.selected = null;
    chess.legal = [];
  }
  renderChess(true);
});
chessBoardElement.addEventListener('keydown', (event) => {
  const index = moveGridFocus(event, chessBoardElement, 8);
  if (index !== null) chessFocus = index;
});
document.querySelector('#newChess').addEventListener('click', newChess);
document.querySelector('#chessMode').addEventListener('change', newChess);
document.querySelector('#chessDifficulty').addEventListener('change', () => { renderChessRating(); newChess(); });
chessSideSelect.addEventListener('change', () => {
  localStorage.setItem('david-pi-chess-side', chessSideSelect.value);
  newChess();
});
document.querySelector('#undoChess').addEventListener('click', () => {
  if (!chessUndo.length) return;
  chess = JSON.parse(chessUndo.pop());
  while (document.querySelector('#chessMode').value === 'solo' && chess.turn !== chess.playerColor && chessUndo.length) chess = JSON.parse(chessUndo.pop());
  chessAiThinking = false;
  chess.over = false;
  const name = chess.turn[0].toUpperCase() + chess.turn.slice(1);
  chessMessage.textContent = `${name} to move.`;
  renderChess();
});

// Checkers
const checkersBoardElement = document.querySelector('#checkersBoard');
const checkersMessage = document.querySelector('#checkersMessage');
let checkers;
let checkersUndo = [];
let checkersAiThinking = false;
let checkersFocus = 0;
const otherCheckersColor = (color) => color === 'coral' ? 'dark' : 'coral';
const checkerDirections = (piece) => piece.king ? [-1, 1] : [piece.color === 'coral' ? -1 : 1];

function initialCheckersBoard() {
  const board = Array(64).fill(null);
  for (let row = 0; row < 3; row += 1) for (let column = 0; column < 8; column += 1) {
    if ((row + column) % 2) board[chessAt(row, column)] = {color:'dark', king:false};
  }
  for (let row = 5; row < 8; row += 1) for (let column = 0; column < 8; column += 1) {
    if ((row + column) % 2) board[chessAt(row, column)] = {color:'coral', king:false};
  }
  return board;
}

function checkerMoves(board, from, capturesOnly = false) {
  const piece = board[from];
  if (!piece) return [];
  const row = Math.floor(from / 8);
  const column = from % 8;
  const moves = [];
  checkerDirections(piece).forEach((rowStep) => [-1, 1].forEach((columnStep) => {
    const nearRow = row + rowStep;
    const nearColumn = column + columnStep;
    if (!chessInside(nearRow, nearColumn)) return;
    const nearIndex = chessAt(nearRow, nearColumn);
    if (!board[nearIndex] && !capturesOnly) moves.push({to:nearIndex, capture:null});
    else if (board[nearIndex] && board[nearIndex].color !== piece.color) {
      const farRow = row + rowStep * 2;
      const farColumn = column + columnStep * 2;
      if (chessInside(farRow, farColumn) && !board[chessAt(farRow, farColumn)]) {
        moves.push({to:chessAt(farRow, farColumn), capture:nearIndex});
      }
    }
  }));
  return capturesOnly ? moves.filter((move) => move.capture !== null) : moves;
}

function checkerTurnCaptures(color) {
  return checkers.board.flatMap((piece, index) => piece?.color === color
    ? checkerMoves(checkers.board, index, true).map((move) => ({...move, from:index})) : []);
}

function legalCheckerMoves(from) {
  const captures = checkerTurnCaptures(checkers.turn);
  return captures.length ? captures.filter((move) => move.from === from)
    : checkerMoves(checkers.board, from);
}

function newCheckers() {
  syncBoardModeControls('checkers');
  checkers = {board:initialCheckersBoard(), turn:'coral', selected:null, legal:[], forced:null, lastMove:null, over:false, moves:0, difficulty:document.querySelector('#checkersDifficulty').value};
  checkersUndo = [];
  checkersAiThinking = false;
  checkersMessage.textContent = 'Coral moves first.';
  beginScoredGame('checkers');
  renderCheckers();
}

function renderCheckers(forceFocus = false) {
  const focusedSquare = checkersBoardElement.contains(document.activeElement)
    ? Number(document.activeElement.closest('[data-index]')?.dataset.index) : null;
  if (focusedSquare !== null && Number.isInteger(focusedSquare)) checkersFocus = focusedSquare;
  const hadFocus = forceFocus || focusedSquare !== null;
  checkersBoardElement.innerHTML = checkers.board.map((piece, index) => {
    const row = Math.floor(index / 8);
    const legal = checkers.legal.some((move) => move.to === index);
    const classes = ['strategy-square', (row + index % 8) % 2 ? 'dark' : ''];
    if (index === checkers.selected) classes.push('selected');
    if (legal) classes.push(piece ? 'capture' : 'legal');
    if (checkers.lastMove?.includes(index)) classes.push('last-move');
    const state = index === checkers.selected ? ', selected' : legal ? (piece ? ', available capture' : ', available move') : '';
    return `<button type="button" class="${classes.join(' ')}" data-index="${index}" role="gridcell" aria-rowindex="${row + 1}" aria-colindex="${index % 8 + 1}" aria-selected="${index === checkers.selected}" tabindex="${index === checkersFocus ? '0' : '-1'}" aria-label="${
      piece ? `${piece.color} ${piece.king ? 'king' : 'checker'}${state}` : `Empty square${state}`}">${
      piece ? `<span class="checkers-piece ${piece.color === 'dark' ? 'dark-piece' : ''} ${piece.king ? 'king' : ''}"></span>` : ''
    }</button>`;
  }).join('');
  document.querySelector('#undoCheckers').disabled = !checkersUndo.length;
  document.querySelector('#checkersMoves').textContent = checkers.moves;
  restoreGridFocus(checkersBoardElement, checkersFocus, hadFocus);
}

function finishCheckerTurn() {
  checkers.turn = otherCheckersColor(checkers.turn);
  checkers.selected = null;
  checkers.legal = [];
  checkers.forced = null;
  const pieces = checkers.board.filter((piece) => piece?.color === checkers.turn);
  const hasMove = pieces.length && checkers.board.some((piece, index) => piece?.color === checkers.turn
    && checkerMoves(checkers.board, index).length);
  if (!hasMove) {
    checkers.over = true;
    const winner = otherCheckersColor(checkers.turn);
    checkersMessage.textContent = `${winner === 'coral' ? 'Coral' : 'Charcoal'} wins!`;
    showToast(checkersMessage.textContent);
    if (document.querySelector('#checkersMode').value === 'solo' && winner === 'coral') {
      registerCompletedGame('checkers', checkers.difficulty, checkers.moves, {mode:'solo', result:'win'});
    }
  } else checkersMessage.textContent = `${checkers.turn === 'coral' ? 'Coral' : 'Charcoal'} to move.`;
}

function chooseCheckersAiMove() {
  const captures = checkerTurnCaptures('dark');
  const moves = captures.length ? captures : checkers.board.flatMap((piece, from) => piece?.color === 'dark'
    ? checkerMoves(checkers.board, from).map((move) => ({...move, from})) : []);
  if (!moves.length) return null;
  if (checkers.difficulty === 'easy') return moves[Math.floor(Math.random() * moves.length)];
  const scored = moves.map((move) => {
    const piece = checkers.board[move.from];
    const targetRow = Math.floor(move.to / 8);
    const promotion = !piece.king && targetRow === 7 ? 8 : 0;
    const center = 4 - Math.abs(3.5 - (move.to % 8));
    let score = (move.capture !== null ? 20 : 0) + promotion + center;
    if (checkers.difficulty === 'hard') {
      const next = checkers.board.map((item) => item ? {...item} : null);
      next[move.to] = next[move.from];
      next[move.from] = null;
      if (move.capture !== null) next[move.capture] = null;
      const opponentCaptures = next.flatMap((item, from) => item?.color === 'coral'
        ? checkerMoves(next, from, true) : []);
      score -= opponentCaptures.length * 7;
    }
    return {move, score:score + Math.random() * (checkers.difficulty === 'hard' ? 1 : 3)};
  }).sort((a, b) => b.score - a.score);
  return scored[0].move;
}

function maybeCheckersAi() {
  if (document.querySelector('#checkersMode').value !== 'solo' || checkers.turn !== 'dark' || checkers.over || checkersAiThinking) return;
  checkersAiThinking = true;
  checkersMessage.textContent = `${window.davidPiServerName || "Home server"} is thinking…`;
  setTimeout(() => {
    const move = checkers.forced !== null
      ? checkerMoves(checkers.board, checkers.forced, true)[0]
      : chooseCheckersAiMove();
    checkersAiThinking = false;
    if (move && !checkers.over && checkers.turn === 'dark') moveChecker(checkers.forced ?? move.from, move);
  }, 320);
}

function moveChecker(from, move) {
  checkersUndo.push(JSON.stringify(checkers));
  checkersFocus = move.to;
  checkers.board[move.to] = checkers.board[from];
  checkers.board[from] = null;
  if (move.capture !== null) checkers.board[move.capture] = null;
  const piece = checkers.board[move.to];
  const row = Math.floor(move.to / 8);
  if ((piece.color === 'coral' && row === 0) || (piece.color === 'dark' && row === 7)) piece.king = true;
  checkers.lastMove = [from, move.to];
  checkers.moves += 1;
  if (move.capture !== null) {
    const more = checkerMoves(checkers.board, move.to, true);
    if (more.length) {
      checkers.selected = move.to;
      checkers.legal = more;
      checkers.forced = move.to;
      checkersMessage.textContent = 'Keep jumping with the same piece.';
      renderCheckers(true);
      maybeCheckersAi();
      return;
    }
  }
  finishCheckerTurn();
  renderCheckers(true);
  maybeCheckersAi();
}

checkersBoardElement.addEventListener('click', (event) => {
  if (checkers.over || checkersAiThinking || (document.querySelector('#checkersMode').value === 'solo' && checkers.turn === 'dark')) return;
  const square = event.target.closest('.strategy-square');
  if (!square) return;
  const index = Number(square.dataset.index);
  checkersFocus = index;
  const move = checkers.legal.find((item) => item.to === index);
  if (checkers.selected !== null && move) {
    moveChecker(checkers.selected, move);
    return;
  }
  const piece = checkers.board[index];
  if (piece?.color === checkers.turn && (checkers.forced === null || checkers.forced === index)) {
    checkers.selected = index;
    checkers.legal = legalCheckerMoves(index);
    if (!checkers.legal.length && checkerTurnCaptures(checkers.turn).length) {
      checkersMessage.textContent = 'A jump is available with another piece.';
    }
  } else if (checkers.forced === null) {
    checkers.selected = null;
    checkers.legal = [];
  }
  renderCheckers(true);
});
checkersBoardElement.addEventListener('keydown', (event) => {
  const index = moveGridFocus(event, checkersBoardElement, 8);
  if (index !== null) checkersFocus = index;
});
document.querySelector('#newCheckers').addEventListener('click', newCheckers);
document.querySelector('#checkersMode').addEventListener('change', newCheckers);
document.querySelector('#checkersDifficulty').addEventListener('change', newCheckers);
document.querySelector('#undoCheckers').addEventListener('click', () => {
  if (!checkersUndo.length) return;
  checkers = JSON.parse(checkersUndo.pop());
  if (document.querySelector('#checkersMode').value === 'solo' && checkers.turn === 'dark' && checkersUndo.length) {
    checkers = JSON.parse(checkersUndo.pop());
  }
  checkers.over = false;
  checkersMessage.textContent = `${checkers.turn === 'coral' ? 'Coral' : 'Charcoal'} to move.`;
  renderCheckers();
});

newSudoku();
newSolitaire();
newMemory();
newChess();
newCheckers();
loadChessRating();
openGame(localStorage.getItem('david-pi-game') || 'sudoku');
