from click.testing import CliRunner

from archaeon import cli


def test_review_builds_app_and_runs_uvicorn(tmp_path, monkeypatch):
    (tmp_path / "CLM-0001.yaml").write_text(
        "id: CLM-0001\ntype: threshold\nstatement: s\nfeature: src/foo\n"
        "status: recovered\nconfidence: 0.5\nsymbols: []\nevidence: []\n"
        "counter_evidence: []\n", encoding="utf-8")
    captured = {}

    def fake_run(app, host, port):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    import uvicorn
    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = CliRunner().invoke(
        cli.main, ["review", "--claims", str(tmp_path), "--port", "9123"])
    assert result.exit_code == 0, result.output
    assert captured["port"] == 9123
    assert captured["host"] == "127.0.0.1"
    # the app exposes the review API
    assert any(getattr(r, "path", "") == "/api/components"
               for r in captured["app"].routes)
