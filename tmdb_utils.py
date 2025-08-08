import requests
import random

API_KEY = "8c95edfae0be4e0773313699e4ee1f8d"  # Replace with your TMDb API key
TMDB_URL = "https://api.themoviedb.org/3"

def get_random_popular_movie():
    page = random.randint(1, 10)
    response = requests.get(f"{TMDB_URL}/movie/popular", params={
        "api_key": API_KEY,
        "language": "en-US",
        "page": page
    })

    data = response.json()
    movie = random.choice(data["results"])

    return {
        "title": movie["title"],
        "overview": movie["overview"],
        "year": movie["release_date"].split("-")[0]
    }
