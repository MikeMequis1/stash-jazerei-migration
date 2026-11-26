import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from src.browser.jazerei import login_evidence
from src.browser.session import BrowserSession, profile_status
from src.config import Config
from src.jazerei.state import read_session_user_id


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - contrato HTTP
        body = b"<html><head><title>local</title></head><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: ANN002 - silencia logs
        return


@pytest.fixture
def local_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_profile_status_empty(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path)
    status = profile_status(config)
    assert status.profile_exists is False
    assert status.apparently_usable is False


def test_profile_status_detects_storage(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path)
    storage = config.resolved_browser_profile / "Default" / "Local Storage" / "leveldb"
    storage.mkdir(parents=True)
    (storage / "000001.log").write_text("x", encoding="utf-8")
    status = profile_status(config)
    assert status.profile_exists
    assert status.has_local_storage
    assert status.apparently_usable


def test_login_evidence_requires_auth_key_not_empty_indexed_db(
    tmp_path: Path, local_site: str
) -> None:
    pytest.importorskip("playwright")
    config = Config(data_dir=tmp_path, browser_headless=True)
    with BrowserSession(config) as session:
        session.goto(local_site)
        session.page.evaluate(
            """
            async () => {
              await new Promise((resolve, reject) => {
                const request = indexedDB.open('firebaseLocalStorageDb', 1);
                request.onupgradeneeded = () => {
                  request.result.createObjectStore('firebaseLocalStorage');
                };
                request.onsuccess = () => resolve();
                request.onerror = () => reject(request.error);
              });
            }
            """
        )
        assert login_evidence(session.page).logged_in is False

        session.page.evaluate(
            """
            () => {
              const request = indexedDB.open('firebaseLocalStorageDb', 1);
              request.onsuccess = () => {
                const db = request.result;
                db.transaction('firebaseLocalStorage', 'readwrite')
                  .objectStore('firebaseLocalStorage')
                  .put({ uid: 'test' }, 'firebase:authUser:test');
              };
            }
            """
        )
        session.wait(500)
        assert login_evidence(session.page).logged_in is True


def test_read_session_user_id_parses_firebase_string_value(tmp_path: Path, local_site: str) -> None:
    pytest.importorskip("playwright")
    config = Config(data_dir=tmp_path, browser_headless=True)
    with BrowserSession(config) as session:
        session.goto(local_site)
        session.page.evaluate(
            """
            async () => {
              await new Promise((resolve, reject) => {
                const request = indexedDB.open('firebaseLocalStorageDb', 1);
                request.onupgradeneeded = () => {
                  request.result.createObjectStore('firebaseLocalStorage');
                };
                request.onsuccess = () => resolve();
                request.onerror = () => reject(request.error);
              });
              await new Promise((resolve, reject) => {
                const request = indexedDB.open('firebaseLocalStorageDb', 1);
                request.onsuccess = () => {
                  const db = request.result;
                  const transaction = db.transaction('firebaseLocalStorage', 'readwrite');
                  transaction.objectStore('firebaseLocalStorage').put(
                    {
                      fbase_key: 'firebase:authUser:test',
                      value: JSON.stringify({ uid: 'string-uid' }),
                    },
                    'firebase:authUser:test',
                  );
                  transaction.oncomplete = () => resolve();
                  transaction.onerror = () => reject(transaction.error);
                };
                request.onerror = () => reject(request.error);
              });
            }
            """
        )
        session.wait(500)
        assert read_session_user_id(session.page) == "string-uid"


def test_browser_open_close_and_persistent_cookie(tmp_path: Path, local_site: str) -> None:
    pytest.importorskip("playwright")
    config = Config(data_dir=tmp_path, browser_headless=True)

    with BrowserSession(config) as session:
        session.goto(local_site)
        assert session.title() == "local"
        session.page.evaluate("localStorage.setItem('persist_test', '1')")
        session.context.add_cookies(
            [
                {
                    "name": "session_test",
                    "value": "1",
                    "url": local_site,
                    "expires": time.time() + 3600,
                }
            ]
        )

    with BrowserSession(config) as session:
        cookies = session.context.cookies(local_site)
        assert any(cookie["name"] == "session_test" for cookie in cookies)
        session.goto(local_site)
        assert session.page.evaluate("localStorage.getItem('persist_test')") == "1"

    status = profile_status(config)
    assert status.profile_exists
    assert status.last_opened
    assert status.last_closed
    assert status.apparently_usable
