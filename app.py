"""Atulya Yatra 2.0 — application entry point.

Run locally with:  python app.py
Or via Flask:       flask --app app run --debug
"""
from atulya import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
