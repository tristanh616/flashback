const socket = io();

// join game
function joinGame(gameId, playerName) {
  socket.emit('join', { game_id: gameId, player: playerName });
}

// submit answer
function sendAnswer(gameId, playerName, answer) {
  socket.emit('submit_answer', {
    game_id: gameId,
    player: playerName,
    answer: answer
  });
}

// host validating answers
function validateCorrectAnswers(gameId, correctPlayers) {
  socket.emit('validate_answers', {
    game_id: gameId,
    correct_players: correctPlayers
  });
}

// ending game
function endGame(gameId) {
  socket.emit('end_game', { game_id: gameId });
}
