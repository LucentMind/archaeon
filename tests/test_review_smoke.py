import socket
import subprocess
import sys
import time

import pytest
import yaml

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write_claim(claims_dir):
    (claims_dir / "CLM-0001.yaml").write_text(yaml.safe_dump({
        "id": "CLM-0001", "type": "threshold", "statement": "temp <= 90",
        "feature": "src/foo", "layer": "what", "status": "machine_verified",
        "confidence": 0.9, "symbols": ["MAX_TEMP"],
        "evidence": [{"kind": "code", "ref": "a.c:1-2", "role": "primary",
                      "excerpt": "if (t > MAX_TEMP)"}],
        "counter_evidence": [],
    }, sort_keys=False), encoding="utf-8")


def _wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def test_browse_and_accept_round_trip(tmp_path):
    _write_claim(tmp_path)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "archaeon.cli", "review",
         "--claims", str(tmp_path), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_port(port)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/")
            page.click("button[data-comp]")           # open the component
            page.click("button[data-cl]")             # open its cluster
            page.wait_for_selector(".card")           # treemap drilled to a card
            page.click(".card")                        # focus it
            page.keyboard.press("a")                   # accept
            page.wait_for_timeout(400)
            browser.close()
        on_disk = yaml.safe_load((tmp_path / "CLM-0001.yaml").read_text())
        assert on_disk["status"] == "expert_accepted"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
