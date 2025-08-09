from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_socketio import SocketIO, emit, join_room
import random
import string
import os


from game_handler import create_game_json, load_game, save_game

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app)

GAMES_PATH = 'games'

# ----- ROUTES -----

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/create', methods=['GET', 'POST'])
def create():
    if request.method == 'POST':
        modes = request.form.getlist('modes')  # list of selected modes
        team_mode = request.form.get('team_mode') == 'on'
        server_name = request.form['server_name']
        # Optional number of questions (defaults to 10 if not provided)
        num_questions = int(request.form.get('num_questions', 10))

        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        create_game_json(code, modes, team_mode, server_name)

        # Trim or cap questions to requested amount (non-breaking even if generator makes more)
        game = load_game(code)
        if game and 'questions' in game:
            game['questions'] = game['questions'][:num_questions]
            # Initialize per-round state
            game['current_question'] = 0
            game['answered_teams'] = []
            game['answers_current'] = {}
            save_game(code, game)

        return redirect(url_for('host_lobby', game_id=code))
    return render_template('create.html')

@app.route('/join', methods=['POST'])
def join():
    username = request.form['username']
    game_id = request.form['game_id'].upper()
    game = load_game(game_id)
    if game:
        return redirect(url_for('player_lobby', game_id=game_id, username=username))
    return "Invalid game code", 404

@app.route('/host/<game_id>')
def host_lobby(game_id):
    return render_template('host_lobby.html', game_id=game_id)

@app.route('/join/<game_id>/<username>')
def player_lobby(game_id, username):
    return render_template('player_lobby.html', game_id=game_id, username=username)

@app.route('/host/<game_id>/game')
def host_game(game_id):
    return render_template('host_game.html', game_id=game_id)

@app.route('/play/<game_id>/<username>')
def player_game(game_id, username):
    return render_template('player_game.html', game_id=game_id, username=username)

# Projector (public display) view
@app.route('/projector/<game_id>')
def projector_view(game_id):
    return render_template('projector_game.html', game_id=game_id)

# Simple API to fetch full game state (for scoreboard refreshes, etc.)
@app.route('/api/game_state/<game_id>')
def api_game_state(game_id):
    game = load_game(game_id)
    if not game:
        return jsonify({'error': 'Invalid game ID'}), 404
    return jsonify(game)

@app.route('/results/<game_id>')
def results(game_id):
    game = load_game(game_id)
    sorted_teams = sorted(
        game["teams"].items(),
        key=lambda item: item[1]["score"],
        reverse=True
    )
    return render_template('results.html', game=game, sorted_teams=sorted_teams)

@app.route('/api/next_question/<game_id>')
def get_next_question(game_id):
    game = load_game(game_id)
    if not game:
        return jsonify({'error': 'Invalid game ID'}), 404

    index = game["current_question"]
    if index >= len(game["questions"]):
        return jsonify({'question': 'No more questions.'})

    q = game["questions"][index]
    return jsonify({'question': q['data']})

# ----- SOCKET.IO EVENTS -----

@socketio.on('join_room')
def on_join(data):
    join_room(data['game_id'])

@socketio.on('join_game')
def on_join_game(data):
    game_id = data['game_id']
    username = data['username']
    join_room(game_id)

    game = load_game(game_id)
    if username not in game['teams']:
        game['teams'][username] = {'score': 0, 'answers': []}
        save_game(game_id, game)

    emit('player_joined', {'username': username}, room=game_id)

@socketio.on('new_answer')
def on_new_answer(data):
    game_id = data['game_id']
    team = data['team']
    answer = data['answer']

    game = load_game(game_id)
    # Ensure per-round containers exist
    game.setdefault('answered_teams', [])
    game.setdefault('answers_current', {})

    # Enforce one answer per team per question
    if team in game['answered_teams']:
        return  # silently ignore repeats

    game['answered_teams'].append(team)
    game['answers_current'][team] = answer
    save_game(game_id, game)

    emit('incoming_answer', {'team': team, 'answer': answer}, room=game_id)

@socketio.on('mark_correct')
def on_mark_correct(data):
    game_id = data['game_id']
    team = data['team']
    game = load_game(game_id)
    if team in game['teams']:
        game['teams'][team]['score'] += 1
        save_game(game_id, game)
        emit('score_updated', {'team': team, 'score': game['teams'][team]['score']}, room=game_id)

@socketio.on('next_question')
def on_next_question(data):
    game_id = data['game_id']
    game = load_game(game_id)

    if game['current_question'] >= len(game['questions']):
        emit('next_question', {'question': None}, room=game_id)
        return

    # Reset round state
    game['answered_teams'] = []
    game['answers_current'] = {}
    game['accepting_answers'] = True

    q = game['questions'][game['current_question']]

    emit('next_question', {
        'question': {
            'id': q.get('id'),
            'mode': q.get('mode'),
            'type': q.get('type'),
            'prompt': q.get('prompt'),
            'media_url': q.get('media_url'),
            'meta': q.get('meta', {})
        },
        'host': {'answer': q.get('answer','')}
    }, room=game_id)

    socketio.emit("start_timer", {"duration": 30}, to=game_id)
    save_game(game_id, game)


@socketio.on('timer_ended')
def on_timer_ended(data):
    """
    When any client signals the timer ended, reveal answer, lock inputs, and auto-score exact matches.
    """
    game_id = data['game_id']
    game = load_game(game_id)

    if game['current_question'] >= len(game['questions']):
        return

    q = game['questions'][game['current_question']]
    correct = str(q['answer']).strip().lower()

    winners = []
    for team, ans in game.get('answers_current', {}).items():
        if str(ans).strip().lower() == correct:
            winners.append(team)
            # award 1 point each; adjust if you want variable scoring
            if team in game['teams']:
                game['teams'][team]['score'] += 1
                emit('score_updated', {'team': team, 'score': game['teams'][team]['score']}, room=game_id)

    # Reveal and lock UI
    emit('reveal_answer', {'correct': q['answer'], 'winners': winners}, room=game_id)
    emit('lock_input', {}, room=game_id)

    # Advance to next question AFTER reveal
    game['current_question'] += 1
    save_game(game_id, game)

@socketio.on('end_game')
def on_end_game(data):
    emit('game_ended', {}, room=data['game_id'])

# ----- MAIN RUN -----

if __name__ == '__main__':
    if not os.path.exists(GAMES_PATH):
        os.makedirs(GAMES_PATH)
    socketio.run(app, debug=True)
