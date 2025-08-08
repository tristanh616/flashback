import json, os

WINNER_FILE = "winners.json"
GAME_FILE = "game_data.json"

def save_winner(game_id, players):
    winners = sorted(players.items(), key=lambda x: x[1], reverse=True)
    result = {"game_id": game_id, "winners": winners}
    try:
        with open(WINNER_FILE, 'r') as f:
            data = json.load(f)
    except:
        data = []

    data.append(result)
    with open(WINNER_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def load_game_data():
    if not os.path.exists(GAME_FILE):
        return {}
    with open(GAME_FILE, 'r') as f:
        return json.load(f)

def save_game_data(data):
    with open(GAME_FILE, 'w') as f:
        json.dump(data, f, indent=2)
