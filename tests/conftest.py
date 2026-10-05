from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from code_review_mcp.config import Settings
from code_review_mcp.hub import ReviewHub
from code_review_mcp.service import ReviewService
from code_review_mcp.store import Store
from code_review_mcp.web import create_app

BASE_URL = "http://127.0.0.1:7791"

SAMPLE_DIFF = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,3 @@
 import os
-x = 1
+x = 2
 print(x)
"""

SAMPLE_NEW_FILE = "import os\nx = 2\nprint(x)\n"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home", host="127.0.0.1", port=7791, open_browser=False)


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    opened = Store.open(settings.db_path)
    yield opened
    opened.close()


@pytest.fixture
def hub() -> ReviewHub:
    return ReviewHub()


@pytest.fixture
def service(store: Store, hub: ReviewHub, settings: Settings) -> ReviewService:
    return ReviewService(store, hub, settings)


@pytest.fixture
def app(settings: Settings, store: Store) -> FastAPI:
    return create_app(settings, store)


@pytest.fixture
def app_service(app: FastAPI) -> ReviewService:
    app_service: ReviewService = app.state.service
    return app_service


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as http_client:
        yield http_client


@pytest.fixture
def diff_file(tmp_path: Path) -> Path:
    path = tmp_path / "review.diff"
    path.write_text(SAMPLE_DIFF)
    return path


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(SAMPLE_NEW_FILE)
    return repo
