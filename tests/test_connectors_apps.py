"""The maintainer command that ships ImuTechz's Google client (synthetic files only)."""
from __future__ import annotations

import io
import json

import pytest

from services.connectors import apps


def test_import_google_writes_local_file_and_never_prints_the_secret(tmp_path, capsys):
    client = tmp_path / "client_secret_1-a.apps.googleusercontent.com.json"
    client.write_text(json.dumps({"installed": {"client_id": "1-a.apps.googleusercontent.com",
                                                "client_secret": "GOCSPX-never-print", "project_id": "p1",
                                                "redirect_uris": ["http://localhost"]}}))
    local = tmp_path / "apps.local.json"
    out = io.StringIO()
    assert apps.main(["import-google", str(client)], local_path=local, out=out) == 0
    assert "GOCSPX" not in out.getvalue() and "GOCSPX" not in capsys.readouterr().err
    assert "client_id=1-a.apps.googleusercontent.com" in out.getvalue()
    stored = json.loads(local.read_text())["gmail"]
    assert stored["client_secret"] == "GOCSPX-never-print" and stored["registered_by"] == "ImuTechz"
    entry = apps.builtin_app("gmail", local_path=local)
    assert entry["client_id"] == "1-a.apps.googleusercontent.com" and entry["redirect_rules"]
    out = io.StringIO()
    apps.main(["list"], local_path=local, out=out)
    assert "1-a.apps.googleusercontent.com" in out.getvalue() and "GOCSPX" not in out.getvalue()
    assert "register a GitHub OAuth App" in out.getvalue()


def test_bad_files_are_refused_without_quoting(tmp_path, capsys):
    bad = tmp_path / "x.json"
    bad.write_text('{"installed": {"client_id": "nope", "client_secret": "GOCSPX-leak"}}')
    assert apps.main(["import-google", str(bad)], local_path=tmp_path / "l.json") == 2
    assert "GOCSPX" not in capsys.readouterr().err
    assert apps.main(["import-google", "relative.json"], local_path=tmp_path / "l.json") == 2
    assert apps.main([], local_path=tmp_path / "l.json") == 2
    with pytest.raises(ValueError):
        apps.parse_google_client(str(tmp_path / "missing.json"))


def test_every_oauth_service_has_a_builtin_slot():
    from services.connectors.setup import SERVICES

    for sid, spec in SERVICES.items():
        if "oauth" in spec.auth:
            assert sid in apps.BUILTIN and apps.BUILTIN[sid]["registered_by"] == "ImuTechz"
            assert apps.BUILTIN[sid]["client_id"] == ""            # nothing secret in the source
        else:
            assert sid not in apps.BUILTIN                          # token-only services
