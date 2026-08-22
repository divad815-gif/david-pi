(function (root, factory) {
  const engine = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = engine;
  else root.ChessEngine = engine;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const at = (row, column) => row * 8 + column;
  const inside = (row, column) => row >= 0 && row < 8 && column >= 0 && column < 8;
  const other = (color) => color === 'white' ? 'black' : 'white';
  const copyBoard = (board) => board.map((piece) => piece ? {...piece} : null);

  function initialBoard() {
    const board = Array(64).fill(null);
    const back = ['rook', 'knight', 'bishop', 'queen', 'king', 'bishop', 'knight', 'rook'];
    back.forEach((type, column) => {
      board[at(0, column)] = {type, color:'black'};
      board[at(1, column)] = {type:'pawn', color:'black'};
      board[at(6, column)] = {type:'pawn', color:'white'};
      board[at(7, column)] = {type, color:'white'};
    });
    return board;
  }

  function initialState() {
    return {
      board: initialBoard(),
      turn: 'white',
      castling: {white:{king:true, queen:true}, black:{king:true, queen:true}},
      enPassant: null,
    };
  }

  function stateFrom(value) {
    return Array.isArray(value)
      ? {board:value, turn:'white', castling:{white:{king:false, queen:false}, black:{king:false, queen:false}}, enPassant:null}
      : value;
  }

  function pseudoMoves(value, from, attacksOnly = false) {
    const state = stateFrom(value);
    const board = state.board;
    const piece = board[from];
    if (!piece) return [];
    const row = Math.floor(from / 8);
    const column = from % 8;
    const moves = [];
    const addStep = (targetRow, targetColumn, captureOnly = false) => {
      if (!inside(targetRow, targetColumn)) return;
      const targetIndex = at(targetRow, targetColumn);
      const target = board[targetIndex];
      if ((captureOnly && target && target.color !== piece.color)
          || (!captureOnly && (!target || target.color !== piece.color))) moves.push(targetIndex);
    };
    const slide = (directions) => directions.forEach(([rowStep, columnStep]) => {
      let targetRow = row + rowStep;
      let targetColumn = column + columnStep;
      while (inside(targetRow, targetColumn)) {
        const targetIndex = at(targetRow, targetColumn);
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
        if (!inside(targetRow, targetColumn)) return;
        const targetIndex = at(targetRow, targetColumn);
        const target = board[targetIndex];
        if (attacksOnly || (target && target.color !== piece.color)
            || (!attacksOnly && state.enPassant?.target === targetIndex
              && board[state.enPassant.pawn]?.type === 'pawn'
              && board[state.enPassant.pawn]?.color !== piece.color)) moves.push(targetIndex);
      });
      if (!attacksOnly) {
        const oneRow = row + direction;
        const one = at(oneRow, column);
        if (inside(oneRow, column) && !board[one]) {
          moves.push(one);
          const startRow = piece.color === 'white' ? 6 : 1;
          const twoRow = row + direction * 2;
          const two = at(twoRow, column);
          if (row === startRow && inside(twoRow, column) && !board[two]) moves.push(two);
        }
      }
    } else if (piece.type === 'knight') {
      [[-2,-1],[-2,1],[-1,-2],[-1,2],[1,-2],[1,2],[2,-1],[2,1]]
        .forEach(([a, b]) => addStep(row + a, column + b));
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

  function squareAttacked(state, square, byColor) {
    return state.board.some((piece, index) => piece?.color === byColor
      && pseudoMoves(state, index, true).includes(square));
  }

  function inCheck(state, color) {
    const king = state.board.findIndex((piece) => piece?.type === 'king' && piece.color === color);
    return king < 0 || squareAttacked(state, king, other(color));
  }

  function revokeRookRight(castling, color, square) {
    const homeRow = color === 'white' ? 7 : 0;
    if (square === at(homeRow, 0)) castling[color].queen = false;
    if (square === at(homeRow, 7)) castling[color].king = false;
  }

  function applyMove(value, move) {
    const state = stateFrom(value);
    const from = move.from;
    const to = move.to;
    const board = copyBoard(state.board);
    const moved = board[from] ? {...board[from]} : null;
    if (!moved) return {...state, board};
    const captured = board[to] ? {...board[to]} : null;
    const castling = {
      white:{...(state.castling?.white || {king:false, queen:false})},
      black:{...(state.castling?.black || {king:false, queen:false})},
    };
    let special = null;

    if (moved.type === 'pawn' && state.enPassant?.target === to && !board[to]
        && Math.abs((to % 8) - (from % 8)) === 1) {
      board[state.enPassant.pawn] = null;
      special = 'en-passant';
    }

    board[to] = moved;
    board[from] = null;

    if (moved.type === 'king') {
      castling[moved.color].king = false;
      castling[moved.color].queen = false;
      if (Math.abs((to % 8) - (from % 8)) === 2) {
        const row = moved.color === 'white' ? 7 : 0;
        const kingSide = (to % 8) === 6;
        const rookFrom = at(row, kingSide ? 7 : 0);
        const rookTo = at(row, kingSide ? 5 : 3);
        board[rookTo] = board[rookFrom] ? {...board[rookFrom]} : null;
        board[rookFrom] = null;
        special = kingSide ? 'castle-kingside' : 'castle-queenside';
      }
    }
    if (moved.type === 'rook') revokeRookRight(castling, moved.color, from);
    if (captured?.type === 'rook') revokeRookRight(castling, captured.color, to);

    const toRow = Math.floor(to / 8);
    if (moved.type === 'pawn' && (toRow === 0 || toRow === 7)) {
      board[to] = {...moved, type:'queen'};
      special = special || 'promotion';
    }

    let enPassant = null;
    if (moved.type === 'pawn' && Math.abs(Math.floor(to / 8) - Math.floor(from / 8)) === 2) {
      enPassant = {target:at((Math.floor(to / 8) + Math.floor(from / 8)) / 2, from % 8), pawn:to};
    }
    return {...state, board, turn:other(state.turn), castling, enPassant, special};
  }

  function castleMoves(state, from) {
    const piece = state.board[from];
    if (!piece || piece.type !== 'king') return [];
    const row = piece.color === 'white' ? 7 : 0;
    if (from !== at(row, 4) || inCheck(state, piece.color)) return [];
    const rights = state.castling?.[piece.color] || {};
    const enemy = other(piece.color);
    const moves = [];
    if (rights.king && state.board[at(row, 7)]?.type === 'rook'
        && state.board[at(row, 7)]?.color === piece.color
        && !state.board[at(row, 5)] && !state.board[at(row, 6)]
        && !squareAttacked(state, at(row, 5), enemy)
        && !squareAttacked(state, at(row, 6), enemy)) moves.push(at(row, 6));
    if (rights.queen && state.board[at(row, 0)]?.type === 'rook'
        && state.board[at(row, 0)]?.color === piece.color
        && !state.board[at(row, 1)] && !state.board[at(row, 2)] && !state.board[at(row, 3)]
        && !squareAttacked(state, at(row, 3), enemy)
        && !squareAttacked(state, at(row, 2), enemy)) moves.push(at(row, 2));
    return moves;
  }

  function legalMoves(state, from) {
    const piece = state.board[from];
    if (!piece) return [];
    const candidates = pseudoMoves(state, from).concat(castleMoves(state, from));
    return candidates.filter((to) => !inCheck(applyMove(state, {from, to}), piece.color));
  }

  function allMoves(state, color) {
    return state.board.flatMap((piece, from) => piece?.color === color
      ? legalMoves(state, from).map((to) => ({from, to})) : []);
  }

  return {at, inside, other, initialBoard, initialState, pseudoMoves, squareAttacked, inCheck, applyMove, legalMoves, allMoves};
}));
