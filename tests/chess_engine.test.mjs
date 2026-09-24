import assert from 'node:assert/strict';
const importedEngine = await import('../static/chess-engine.js');
const E = importedEngine.default || globalThis.ChessEngine;
const emptyBoard = () => Array(64).fill(null);
const bareState = (board, turn = 'white') => ({
  board,
  turn,
  castling:{white:{king:true, queen:true}, black:{king:true, queen:true}},
  enPassant:null,
});

{
  const board = emptyBoard();
  board[E.at(7,4)] = {type:'king', color:'white'};
  board[E.at(7,0)] = {type:'rook', color:'white'};
  board[E.at(7,7)] = {type:'rook', color:'white'};
  board[E.at(0,4)] = {type:'king', color:'black'};
  const state = bareState(board);
  const legal = E.legalMoves(state, E.at(7,4));
  assert.ok(legal.includes(E.at(7,2)), 'white can castle queenside');
  assert.ok(legal.includes(E.at(7,6)), 'white can castle kingside');

  const castled = E.applyMove(state, {from:E.at(7,4), to:E.at(7,6)});
  assert.equal(castled.special, 'castle-kingside');
  assert.deepEqual(castled.board[E.at(7,5)], {type:'rook', color:'white'});
  assert.equal(castled.board[E.at(7,7)], null);
  assert.equal(castled.castling.white.king, false);
  assert.equal(castled.castling.white.queen, false);
}

{
  const board = emptyBoard();
  board[E.at(7,4)] = {type:'king', color:'white'};
  board[E.at(7,7)] = {type:'rook', color:'white'};
  board[E.at(0,4)] = {type:'king', color:'black'};
  board[E.at(0,5)] = {type:'rook', color:'black'};
  const state = bareState(board);
  assert.ok(!E.legalMoves(state, E.at(7,4)).includes(E.at(7,6)), 'cannot castle through check');
}

{
  let state = E.initialState();
  state = E.applyMove(state, {from:E.at(6,4), to:E.at(4,4)}); // e2-e4
  state = E.applyMove(state, {from:E.at(1,0), to:E.at(2,0)}); // a7-a6
  state = E.applyMove(state, {from:E.at(4,4), to:E.at(3,4)}); // e4-e5
  state = E.applyMove(state, {from:E.at(1,3), to:E.at(3,3)}); // d7-d5
  assert.ok(E.legalMoves(state, E.at(3,4)).includes(E.at(2,3)), 'en passant is legal immediately');
  state = E.applyMove(state, {from:E.at(3,4), to:E.at(2,3)});
  assert.equal(state.special, 'en-passant');
  assert.equal(state.board[E.at(3,3)], null, 'captured pawn is removed');
  assert.deepEqual(state.board[E.at(2,3)], {type:'pawn', color:'white'});
}

{
  let state = E.initialState();
  state = E.applyMove(state, {from:E.at(6,4), to:E.at(4,4)});
  state = E.applyMove(state, {from:E.at(1,0), to:E.at(2,0)});
  state = E.applyMove(state, {from:E.at(4,4), to:E.at(3,4)});
  state = E.applyMove(state, {from:E.at(1,3), to:E.at(3,3)});
  state = E.applyMove(state, {from:E.at(7,6), to:E.at(5,5)}); // decline en passant
  state = E.applyMove(state, {from:E.at(2,0), to:E.at(3,0)});
  assert.ok(!E.legalMoves(state, E.at(3,4)).includes(E.at(2,3)), 'en passant expires after one reply');
}

console.log('Chess engine: castling and en-passant tests passed.');
