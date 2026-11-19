"""Runtime ASGI entry point.

Keeping application construction here lets ``app.main`` remain import-safe for
tests, tooling, and alternative dependency injection setups.
"""

from app.main import create_app


app = create_app()
