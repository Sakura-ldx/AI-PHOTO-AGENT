#!/usr/bin/env python3
"""Fail when working-tree content or git metadata contains personal data or secrets.

The scanner is intentionally heuristic: it catches common Windows profile paths,
personal photo-library paths, machine identifiers, private network addresses,
free-mail addresses and well-known API key prefixes. It is a guard rail, not a
replacement for review.

Tracked, modified and non-ignored untracked files are scanned, so a file must be
checked before it is staged rather than only after it enters the index.

Usage:
    python scripts/check_privacy.py            # current working tree
    python scripts/check_privacy.py --history  # also scan git history metadata
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Build sensitive fragments at runtime so this scanner does not flag itself.
_DRIVE = r"[A-Za-z]"
_USER_DIR = "C:" + r"\\Users\\"
_PHOTO_MARKER = "摄" + "影"
_REPO_MARKER = "AI-" + "PHOTO-AGENT"
_SID_PREFIX = "S-1-" + "5-21-"
_MAIL_DOMAINS = ("qq", "163", "126", "gmail", "outlook", "hotmail")
_MAIL_PATTERN = r"\b[\w.+-]+@(?:" + "|".join(_MAIL_DOMAINS) + r")\.com\b"

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "windows-user-path",
        re.compile(
            _USER_DIR + r"(?!<user>|<name>|Public\\|Default\\|evil\\)[^\\\s]+",
            re.IGNORECASE,
        ),
    ),
    (
        "private-ipv4",
        re.compile(
            r"\b(?:"
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|192\.168\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}"
            r"|169\.254\.\d{1,3}\.\d{1,3}"
            r")\b"
        ),
    ),
    (
        "photo-library-path",
        re.compile(_DRIVE + r":\\[^\\\r\n]*" + _PHOTO_MARKER, re.IGNORECASE),
    ),
    (
        "repo-absolute-path",
        re.compile(
            _DRIVE + r":\\[^\\\r\n]*" + _REPO_MARKER,
            re.IGNORECASE,
        ),
    ),
    (
        "windows-sid",
        re.compile(r"\b" + _SID_PREFIX + r"\d{9,10}-\d{9,10}-\d{9,10}-\d{4,5}\b"),
    ),
    ("personal-email", re.compile(_MAIL_PATTERN, re.IGNORECASE)),
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        ),
    ),
    (
        "api-key",
        re.compile(
            r"\b(?:"
            r"sk-[A-Za-z0-9_-]{20,}"
            r"|ghp_[A-Za-z0-9]{30,}"
            r"|AIza[0-9A-Za-z_-]{30,}"
            r"|AKIA[0-9A-Z]{16}"
            r"|xox[baprs]-[A-Za-z0-9-]{10,}"
            r")\b"
        ),
    ),
)

FORBIDDEN_HISTORY_FILES = (
    "review-state.json",
    "bridge-path.txt",
    "Config.local.lua",
    ".env",
)


@dataclass(frozen=True)
class Finding:
    location: str
    line: int
    rule: str
    excerpt: str

    def format(self) -> str:
        return f"{self.location}:{self.line}: [{self.rule}] {self.excerpt}"


def scan_text(location: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            excerpt = match.group(0)
            if len(excerpt) > 120:
                excerpt = excerpt[:117] + "..."
            findings.append(
                Finding(
                    location=location,
                    line=line_number,
                    rule=rule,
                    excerpt=excerpt,
                )
            )
    return findings


def scan_path(path: Path, *, root: Path = ROOT) -> list[Finding]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in raw:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    try:
        location = path.relative_to(root).as_posix()
    except ValueError:
        location = str(path)
    return scan_text(location, text)


def repository_files(root: Path = ROOT) -> list[Path]:
    """Return tracked files plus non-ignored untracked working-tree files."""

    result = _git(
        root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    if result is None:
        return []
    names = [name for name in result.split("\x00") if name]
    return [root / name for name in names]


def scan_repository(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in repository_files(root):
        relative = path.relative_to(root) if path.is_absolute() else path
        findings.extend(scan_path(path, root=root))
        if relative.name in {".env", "Config.local.lua"}:
            findings.append(
                Finding(
                    location=relative.as_posix(),
                    line=1,
                    rule="forbidden-file",
                    excerpt=relative.name,
                )
            )
    return findings


def scan_history(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []

    authors = _git(root, "log", "--all", "--format=%ae")
    if authors:
        for email in sorted(set(authors.splitlines())):
            findings.extend(scan_text("git:author", email))

    messages = _git(root, "log", "--all", "--format=%B")
    if messages:
        findings.extend(scan_text("git:message", messages))

    changed = _git(root, "log", "--all", "--name-only", "--format=")
    if changed:
        for name in sorted(set(changed.splitlines())):
            if Path(name).name in FORBIDDEN_HISTORY_FILES:
                findings.append(
                    Finding(
                        location="git:history",
                        line=1,
                        rule="forbidden-history-file",
                        excerpt=name,
                    )
                )
    return findings


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        action="store_true",
        help="also inspect author emails, commit messages and historical file names",
    )
    args = parser.parse_args(argv)

    findings = scan_repository(ROOT)
    if args.history:
        findings.extend(scan_history(ROOT))

    if not findings:
        print("Privacy scan passed: no personal paths or secrets found.")
        return 0

    print(f"Privacy scan failed: {len(findings)} finding(s).", file=sys.stderr)
    for finding in findings:
        print(f"  {finding.format()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
