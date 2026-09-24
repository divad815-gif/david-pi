import gzip

from flask import Flask, Response, jsonify

from modules.security import init_security


def compression_app(tmp_path):
    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "large.js").write_text("const value = 'compressible';\n" * 200)
    app = Flask(__name__, static_folder=str(static_root), static_url_path="/static")
    init_security(app)

    @app.get("/json")
    def large_json():
        return jsonify(value="compressible response " * 200)

    @app.get("/small")
    def small_text():
        return "small"

    @app.get("/binary")
    def binary():
        return Response(b"\x00" * 4096, mimetype="application/octet-stream")

    @app.get("/partial")
    def partial():
        return Response(
            b"x" * 2048, status=206, mimetype="text/plain",
            headers={"Content-Range": "bytes 0-2047/4096"},
        )

    return app


def get(client, path, encoding=None, method="GET"):
    headers = {"Host": "test.localhost"}
    if encoding is not None:
        headers["Accept-Encoding"] = encoding
    return client.open(path, method=method, headers=headers)


def test_large_json_and_static_javascript_are_gzipped(tmp_path):
    client = compression_app(tmp_path).test_client()
    for path in ("/json", "/static/large.js"):
        response = get(client, path, "gzip")
        assert response.status_code == 200
        assert response.headers["Content-Encoding"] == "gzip"
        assert "Accept-Encoding" in response.headers["Vary"]
        assert len(gzip.decompress(response.data)) > len(response.data)
        assert "private" in response.headers["Cache-Control"]


def test_identity_representation_varies_but_small_binary_and_partial_do_not(tmp_path):
    client = compression_app(tmp_path).test_client()
    identity = get(client, "/json")
    assert "Content-Encoding" not in identity.headers
    assert "Accept-Encoding" in identity.headers["Vary"]

    refused = get(client, "/json", "gzip;q=0, identity;q=1")
    assert "Content-Encoding" not in refused.headers
    assert get(client, "/small", "gzip").headers.get("Content-Encoding") is None
    assert get(client, "/binary", "gzip").headers.get("Content-Encoding") is None
    assert get(client, "/partial", "gzip").headers.get("Content-Encoding") is None
    assert get(client, "/json", "gzip", method="HEAD").headers.get("Content-Encoding") is None
