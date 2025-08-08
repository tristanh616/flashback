from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, join_room, emit
from winner_log import save_winner, load_game_data, save_game_data
import uuid
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = 'flashback-secret'
socketio = SocketIO(app)

# — ROUTES —

@app.route('/')
def index():
    return render_template("index.html")

@app.route('/host', methods=['GET', 'POST'])
def host():
    if request.method == 'POST':
        game_id = str(uuid.uuid4())[:5].upper()
        session['game_id'] = game_id
        session['mode'] = request.form['mode']
        session['num_questions'] = int(request.form['num_questions'])

        # Initialiser le jeu dans le fichier
        game_data = load_game_data()
        game_data[game_id] = {
            'players': {},
            'answers': {},
            'current_question': None,
            'correct_answer': "",
            'scoreboard': {}
        }
        save_game_data(game_data)

        return redirect(url_for('lobby', game_id=game_id))

    return render_template("host.html")

@app.route('/lobby/<game_id>')
def lobby(game_id):
    return render_template("lobby.html", game_id=game_id)

@app.route('/screen/<game_id>')
def screen(game_id):
    game_data = load_game_data()
    if game_id not in game_data:
        return "Invalid game ID."
    return render_template("screen.html", game_id=game_id, players=game_data[game_id]['players'])

@app.route('/game/<game_id>/<player>')
def game(game_id, player):
    return render_template("game.html", game_id=game_id, player=player)

@app.route('/end', methods=['POST'])
def end():
    winners = request.form.getlist('winners')
    score = int(request.form['score'])
    game_id = session.get('game_id')
    game_data = load_game_data()

    if game_id and game_id in game_data:
        save_winner(
            game_id=game_id,
            mode=session.get('mode', 'unknown'),
            players=list(game_data[game_id]['players'].keys()),
            winners=winners,
            score=score
        )
        del game_data[game_id]
        save_game_data(game_data)

    return render_template('result.html', winners=winners, score=score)

# — SOCKET EVENTS —

@socketio.on('create_game')
def handle_create_game(data):
    game_id = str(uuid.uuid4())[:5].upper()
    game_data = load_game_data()
    game_data[game_id] = {
        'players': {},
        'answers': {},
        'current_question': None,
        'correct_answer': "",
        'scoreboard': {}
    }
    save_game_data(game_data)
    emit('game_created', {'game_id': game_id})

@socketio.on('join')
def handle_join(data):
    game_id = data['game_id']
    name = data['player']
    join_room(game_id)
    game_data = load_game_data()
    if game_id not in game_data:
        return
    game_data[game_id]['players'][name] = 0
    save_game_data(game_data)
    emit('player_joined', {'player': name}, room=game_id)

@socketio.on('submit_answer')
def handle_answer(data):
    game_id = data['game_id']
    player = data['player']
    answer = data['answer']
    game_data = load_game_data()
    game_data[game_id]['answers'][player] = answer
    save_game_data(game_data)
    emit('answer_received', {'player': player}, room=game_id)

@socketio.on('validate_answers')
def validate_answers(data):
    game_id = data['game_id']
    correct_players = data['correct_players']
    game_data = load_game_data()
    for player in correct_players:
        game_data[game_id]['players'][player] += 1
    save_game_data(game_data)
    emit('update_scores', game_data[game_id]['players'], room=game_id)

@socketio.on('start_question')
def handle_start_question(data):
    game_id = data['game_id']
    question = data['question']
    correct_answer = data['correct_answer']

    game_data = load_game_data()
    game_data[game_id]['current_question'] = question
    game_data[game_id]['correct_answer'] = correct_answer
    game_data[game_id]['answers'] = {}
    save_game_data(game_data)

    emit('display_question', {'question': question}, room=game_id)

@socketio.on('end_game')
def handle_end_game(data):
    game_id = data['game_id']
    game_data = load_game_data()
    if game_id in game_data:
        save_winner(game_id, game_data[game_id]['players'])
        del game_data[game_id]
        save_game_data(game_data)

# — LANCEMENT —
if __name__ == '__main__':
    socketio.run(app, debug=True)
