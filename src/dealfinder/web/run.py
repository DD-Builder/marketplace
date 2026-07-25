"""``dealfinder-web`` entrypoint — runs the dashboard via uvicorn."""

from __future__ import annotations

import uvicorn

from dealfinder.logging import configure_logging


def main() -> None:
    configure_logging()
    uvicorn.run(
        "dealfinder.web.app:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
