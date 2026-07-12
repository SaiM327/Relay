"""Dev entrypoint: python run.py (expose via cloudflared/ngrok for Slack)."""

import logging

from app.config import settings
from app.slack.events import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = create_app()

if __name__ == "__main__":
    app.run(port=settings.port, debug=True)
