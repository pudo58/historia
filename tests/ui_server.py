"""Manual browser smoke fixture. Separate temp DB; no real SSH credentials or GPU."""
import tempfile
from pathlib import Path

import uvicorn

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore

if __name__ == "__main__":
    directory = Path(tempfile.mkdtemp(prefix="historia-ui-smoke-"))
    app = create_app(Settings(database_url=f"sqlite:///{directory / 'test.db'}", studio_root=directory / "data",
                              local_origin="http://127.0.0.1:8001"),
                     SecretStore("isolated-ui-test-only"), lambda host, secret: FakeExecutor())
    uvicorn.run(app, host="127.0.0.1", port=8001)
