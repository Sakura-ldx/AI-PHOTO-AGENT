from __future__ import annotations

import os

from app.env_file import load_env_file, parse_env_file


def test_parse_env_file_supports_comments_quotes_export_and_bom(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "\ufeff# comment\n"
        "A=plain\n"
        "B='single value'\n"
        'C="line\\nbreak"\n'
        "export D=exported\n"
        "E=\n"
        "INVALID\n"
        "1BAD=x\n",
        encoding="utf-8",
    )

    values = parse_env_file(path)

    assert values == {
        "A": "plain",
        "B": "single value",
        "C": "line\nbreak",
        "D": "exported",
        "E": "",
    }


def test_load_env_file_keeps_process_environment_by_default(monkeypatch, tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=from-file\nB=from-file\n", encoding="utf-8")
    monkeypatch.setenv("A", "from-process")
    monkeypatch.delenv("B", raising=False)

    load_env_file(path)

    assert os.environ["A"] == "from-process"
    assert os.environ["B"] == "from-file"


def test_load_env_file_can_override_process_environment(monkeypatch, tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=from-file\n", encoding="utf-8")
    monkeypatch.setenv("A", "from-process")

    load_env_file(path, override=True)

    assert os.environ["A"] == "from-file"


def test_parse_env_file_returns_empty_for_missing_file(tmp_path) -> None:
    assert parse_env_file(tmp_path / "missing.env") == {}
