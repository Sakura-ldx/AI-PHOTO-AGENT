from pathlib import Path

import scripts.check_privacy as privacy
from scripts.check_privacy import ROOT, scan_repository, scan_text


def test_scan_flags_windows_profile_path() -> None:
    text = "see " + "C:" + "\\Users\\" + "alice" + "\\Pictures\\photo.jpg"
    findings = scan_text("docs/sample.md", text)
    assert any(finding.rule == "windows-user-path" for finding in findings)


def test_scan_allows_placeholder_profile_path() -> None:
    text = "see " + "C:" + "\\Users\\" + "<user>" + "\\Pictures\\photo.jpg"
    assert scan_text("docs/sample.md", text) == []


def test_scan_flags_private_network_address() -> None:
    text = "Lightroom bridge: " + "192.168." + "1.20:8000"
    findings = scan_text("docs/sample.md", text)
    assert any(finding.rule == "private-ipv4" for finding in findings)


def test_scan_allows_loopback_address() -> None:
    text = "Lightroom bridge: " + "127.0.0.1:8000"
    assert scan_text("docs/sample.md", text) == []


def test_scan_flags_internal_photo_library_path() -> None:
    text = "upload " + "E:" + "\\" + "摄" + "影" + "\\2026\\example.CR3"
    findings = scan_text("docs/sample.md", text)
    assert any(finding.rule == "photo-library-path" for finding in findings)


def test_scan_flags_machine_sid() -> None:
    text = "owner " + "S-1-" + "5-21-1111111111-2222222222-3333333333-1001"
    findings = scan_text("docs/sample.md", text)
    assert any(finding.rule == "windows-sid" for finding in findings)


def test_scan_flags_api_key() -> None:
    text = "key=sk-" + "a" * 32
    findings = scan_text("docs/sample.md", text)
    assert any(finding.rule == "api-key" for finding in findings)


def test_scan_ignores_short_test_key() -> None:
    text = "DEEPSEEK_API_KEY: sk-from-dsh"
    assert scan_text("tests/sample.py", text) == []


def test_scan_includes_untracked_working_tree_files(
    tmp_path: Path, monkeypatch
) -> None:
    tracked = tmp_path / "tracked.md"
    tracked.write_text("clean\n", encoding="utf-8")
    untracked = tmp_path / "notes.md"
    untracked.write_text("C:" + "\\Users\\" + "alice\\photo.jpg\n", encoding="utf-8")

    calls: list[tuple[str, ...]] = []

    def fake_git(_root: Path, *args: str) -> str:
        calls.append(args)
        return "tracked.md\x00notes.md\x00"

    monkeypatch.setattr(privacy, "_git", fake_git)

    findings = scan_repository(tmp_path)

    assert any(finding.location == "notes.md" for finding in findings)
    assert calls
    assert "--others" in calls[0]
    assert "--exclude-standard" in calls[0]


def test_repository_has_no_privacy_findings() -> None:
    findings = scan_repository(ROOT)
    assert findings == [], "\n".join(finding.format() for finding in findings)
