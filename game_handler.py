import os
import json
from api_fetcher import generate_questions_for_mode

GAMES_PATH = 'games'

def create_game_json(code, modes, team_mode, server_name):
    questions = []
    for mode in modes:
        if mode == 'random':
            from random import choice
            mode = choice(['movie_quote', 'movie_poster', 'music_normal', 'music_reversed'])

        # Use your own logic here
        from api_fetcher import generate_questions_for_mode
        questions += generate_questions_for_mode(mode, count=2)

    game_data = {
        "code": code,
        "server_name": server_name,
        "modes": modes,
        "team_mode": team_mode,
        "teams": {},
        "questions": questions,
        "current_question": 0,
        "history": []
    }
    with open(os.path.join(GAMES_PATH, f"game_{code}.json"), 'w') as f:
        json.dump(game_data, f, indent=4)


def load_game(code):
    try:
        with open(os.path.join(GAMES_PATH, f"game_{code}.json"), 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return None

def save_game(code, data):
    with open(os.path.join(GAMES_PATH, f"game_{code}.json"), 'w') as f:
        json.dump(data, f, indent=4)
