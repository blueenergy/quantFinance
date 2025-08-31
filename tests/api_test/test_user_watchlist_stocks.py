import requests

BASE_URL = "http://localhost:3001"

# 1. Login to get token
login_data = {
    "username": "shuyolin",  # replace with your username
    "password": "linsy163"   # replace with your password
}
login_resp = requests.post(f"{BASE_URL}/api/login", json=login_data)
login_resp.raise_for_status()
token = login_resp.json()["access_token"]

# 2. Use token to call /api/user/watchlist-stocks
headers = {"Authorization": f"Bearer {token}"}
resp = requests.get(f"{BASE_URL}/api/user/watchlist-stocks", headers=headers)
resp.raise_for_status()
print(resp.json())