from scripts.check_privacy import ROOT, scan_repository, scan_text


def test_scan_flags_windows_profile_path() -> None:
    text = "see " + "C:" + "\\Users\\" + "alice" + "\\Pictures\\photo.jpg"
    findings = scan_text("docs/sample.md", text)
    assert any(finding.rule == "windows-user-path" for finding in findings)


def test_scan_allows_placeholder_profile_path() -> None:
    text = "see " + "C:" + "\\Users\\" + "<user>" + "\\Pictures\\photo.jpg"
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


def test_repository_has_no_privacy_findings() -> None:
    findings = scan_repository(ROOT)
    assert findings == [], "\n".join(finding.format() for finding in findings)
