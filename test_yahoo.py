import yfinance as yf
import requests
import json

def test_yahoo_finance():
    print("Testing basic requests to yahoo.com...")
    try:
        resp = requests.get("https://finance.yahoo.com", timeout=5)
        print(f"requests status: {resp.status_code}")
    except Exception as e:
        print(f"requests failed: {e}")

    print("\nTesting yfinance fetching FXI...")
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        })
        ticker = yf.Ticker("FXI", session=session)
        hist = ticker.history(period="1d")
        if not hist.empty:
            print("Successfully fetched FXI:")
            print(hist[['Close']])
        else:
            print("History is empty.")
    except Exception as e:
        print(f"yfinance failed: {e}")

if __name__ == "__main__":
    test_yahoo_finance()
