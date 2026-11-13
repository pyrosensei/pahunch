from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def frontend_client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    monkeypatch.setattr(main, "_FRONTEND_DIST", dist)
    monkeypatch.setattr(main, "build_llm_client", lambda _: None)

    def make_client(*, built):
        if built:
            (dist / "assets").mkdir(parents=True)
            (dist / "index.html").write_text("<html>React app shell</html>", encoding="utf-8")
            (dist / "assets" / "app.js").write_text("console.log('React asset');", encoding="utf-8")
        return TestClient(main.create_app(
            init_database=False,
            speech_client=None,
            streaming_stt_client=None,
        ))

    return make_client


@pytest.mark.parametrize("path", ["/", "/dashboard", "/shop", "/voice", "/call", "/call?session_id=example&order_id=1042"])
def test_built_app_routes_serve_the_react_shell(frontend_client, path):
    with frontend_client(built=True) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.text == "<html>React app shell</html>"
    assert response.headers["content-type"].startswith("text/html")


def test_built_root_order_links_forward_the_complete_query(frontend_client):
    with frontend_client(built=True) as client:
        response = client.get(
            "/?order_id=1042&tab=events&filter=a%2Fb",
            follow_redirects=False,
        )
        assert response.status_code == 307
        assert response.headers["location"] == "/dashboard?order_id=1042&tab=events&filter=a%2Fb"
        assert client.get(response.headers["location"]).text == "<html>React app shell</html>"
        assert client.get("/?tab=events", follow_redirects=False).status_code == 200


def test_built_assets_are_served_before_the_legacy_root_mount(frontend_client):
    with frontend_client(built=True) as client:
        response = client.get("/assets/app.js")
        missing = client.get("/assets/missing.js")
    assert response.status_code == 200
    assert response.text == "console.log('React asset');"
    assert missing.status_code == 404
    assert "React app shell" not in missing.text


@pytest.mark.parametrize("built", [False, True])
@pytest.mark.parametrize("filename", [
    "index.html", "dashboard.html", "shop.html", "worklet.js", "pcm-player.js",
])
def test_legacy_pages_and_audio_worklets_remain_available(frontend_client, built, filename):
    with frontend_client(built=built) as client:
        response = client.get(f"/{filename}")
    legacy = Path(main.__file__).resolve().parent.parent / "static" / filename
    assert response.status_code == 200
    assert response.content == legacy.read_bytes()


@pytest.mark.parametrize("path,filename", [
    ("/", "dashboard.html"),
    ("/dashboard", "dashboard.html"),
    ("/shop", "shop.html"),
    ("/voice", "index.html"),
])
def test_unbuilt_frontend_falls_back_to_legacy_pages(frontend_client, path, filename):
    with frontend_client(built=False) as client:
        response = client.get(path)
        if path == "/":
            assert client.get("/?order_id=1042", follow_redirects=False).status_code == 200
    legacy = Path(main.__file__).resolve().parent.parent / "static" / filename
    assert response.status_code == 200
    assert response.content == legacy.read_bytes()


@pytest.mark.parametrize("built", [False, True])
def test_spa_does_not_swallow_missing_api_or_arbitrary_paths(frontend_client, built):
    with frontend_client(built=built) as client:
        assert client.get("/health").json() == {"status": "ok"}
        for path in ("/api/not-a-real-endpoint", "/not-a-react-route", "/assets/missing.js"):
            response = client.get(path)
            assert response.status_code == 404
            assert response.headers["content-type"].startswith("application/json")
            assert "React app shell" not in response.text
