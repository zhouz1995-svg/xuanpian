"""Redacted, read-only checks for a candidate public source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path, PurePosixPath


Finding = dict[str, object]

_EXEMPTIONS: dict[tuple[str, str], str] = {
    (
        "tests/test_public_source_audit.py",
        "user-path",
    ): "Synthetic user path used to verify redaction and exact-rule exemptions.",
    (
        "tests/test_public_source_audit.py",
        "secret-assignment",
    ): "Synthetic assignment used to verify redaction and CLI failure behavior.",
    (
        "tests/test_public_source_audit.py",
        "private-key",
    ): "Synthetic key header used to verify binary content scanning and redaction.",
    (
        "docs/public-handover-template.md",
        "internal-report",
    ): "Reviewed public-facing handover template containing no project-specific data.",
}

# Each entry is an exact (relative path, rule) exemption for one reviewed full
# file. Any edit changes the digest and restores the finding. Do not exempt
# credential-format or private-key findings from these production/test files.
_REVIEWED_EXEMPTIONS: dict[tuple[str, str], tuple[str, str]] = {
    (
        "src/xuanpian/cache/thumbnail_cache.py",
        "secret-assignment",
    ): (
        "899ae1a43d4d0c6b6971d75622291d1470c60a73861bdca8584d7d845c60c5c1",
        "Reviewed cancellation-token annotations and runtime state, not credentials.",
    ),
    (
        "src/xuanpian/tasks/analysis_coordinator.py",
        "secret-assignment",
    ): (
        "58d25d6d5aff0c79a22a000cc041eca2a6a2490b4defb40d21c212af7385de74",
        "Reviewed runtime cancellation-token construction, not a credential.",
    ),
    (
        "src/xuanpian/ui/async_preview_loader.py",
        "secret-assignment",
    ): (
        "d91a708d2200db0aabf47fb9c1570e53112318846f6711b9421a84d1ff2fe5b3",
        "Reviewed runtime cancellation-token annotation, not a credential.",
    ),
    (
        "src/xuanpian/ui/preview_widget.py",
        "secret-assignment",
    ): (
        "c0610510c378845f9f6b61f2ffca3f23a5d661cdf49111e57b8c9e1799df4687",
        "Reviewed preview cancellation-token annotations and attributes, not credentials.",
    ),
    (
        "tests/test_packaging_metadata.py",
        "secret-assignment",
    ): (
        "d3293fa074ac2553cd3954ec7e53e2347c4e5cce493048d7b1714ecae0bdedd2",
        "Reviewed test comparison of tokenized metadata, not a stored secret.",
    ),
    (
        "tests/test_task_contracts.py",
        "secret-assignment",
    ): (
        "7d822127ef499927115d3db4860f1850bcb0fd041a6aeb252cd3a58edae22955",
        "Reviewed cancellation-token contract test, not a credential.",
    ),
    (
        "tests/test_macos_bundle.py",
        "user-path",
    ): (
        "fb68be11c23b37af6505e482813e4a42148e88385e14aa01210c867beddad830",
        "Reviewed synthetic mock dependency/runpath paths; owner is synthetic-test-user.",
    ),
}

_EXCLUDED_DIRECTORIES = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "venv",
}

_CREDENTIAL_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,255}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{30,255}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"ASIA[0-9A-Z]{16}"),
    re.compile(rb"xox(?:a|b|p|r|s)-[A-Za-z0-9-]{10,}"),
    re.compile(rb"AIza[0-9A-Za-z_-]{35}"),
)

_PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    rb"(?im)^[ \t]*(?:export[ \t]+)?"
    rb"(?:api[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret|"
    rb"password|passwd|secret|token)"
    rb"[ \t]*[:=][ \t]*(?P<value>[^\r\n]{4,})"
)
_USER_PATH_PATTERNS = (
    re.compile(rb"/(?:Users|home)/(?P<owner>[^/\s'\"\\]+)/"),
    re.compile(rb"[A-Za-z]:(?:\\\\|\\)Users(?:\\\\|\\)(?P<owner>[^\\\s'\"]+)(?:\\\\|\\)"),
)
_PATH_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|secret|token)(\s*[:=]\s*)([^/\\]+)"
)
_PATH_PRIVATE_KEY_PATTERN = re.compile(
    r"(?i)-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
_PATH_POSIX_HOME_PATTERN = re.compile(r"(?i)(^|/)(Users|home)/([^/]+)")
_PATH_WINDOWS_HOME_PATTERN = re.compile(r"(?i)([A-Za-z]:(?:\\\\|\\)Users(?:\\\\|\\))([^\\]+)")

_BINARY_SIGNATURES = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"SQLite format 3\x00",
    b"%PDF-",
    b"PK\x03\x04",
)

_PROVENANCE_BINARY_SUFFIXES = {
    ".bmp",
    ".gif",
    ".icns",
    ".ico",
    ".jpeg",
    ".jpg",
    ".onnx",
    ".pb",
    ".png",
    ".task",
    ".tflite",
    ".webp",
}


def _redact_path(path: str, sensitive_values: tuple[bytes, ...] = ()) -> str:
    safe_path = path.encode("utf-8")
    for pattern in _CREDENTIAL_PATTERNS:
        safe_path = pattern.sub(b"[REDACTED]", safe_path)
    for value in sorted(sensitive_values, key=len, reverse=True):
        if value:
            safe_path = safe_path.replace(value, b"[REDACTED]")
    safe_text = safe_path.decode("utf-8")

    def redact_assignment(match: re.Match[str]) -> str:
        suffix = PurePosixPath(match.group(3)).suffix
        return f"{match.group(1)}{match.group(2)}[REDACTED]{suffix}"

    safe_text = _PATH_SECRET_ASSIGNMENT_PATTERN.sub(redact_assignment, safe_text)
    safe_text = _PATH_PRIVATE_KEY_PATTERN.sub("[REDACTED]", safe_text)
    safe_text = _PATH_POSIX_HOME_PATTERN.sub(r"\1\2/[REDACTED]", safe_text)
    return _PATH_WINDOWS_HOME_PATTERN.sub(r"\1[REDACTED]", safe_text)


def _finding(
    category: str,
    path: str,
    severity: str,
    line: int | None = None,
    *,
    path_redactions: tuple[bytes, ...] = (),
) -> Finding:
    result: Finding = {
        "category": category,
        "path": _redact_path(path, path_redactions),
        "severity": severity,
    }
    if line is not None:
        result["line"] = line
    return result


def _is_exempt(relative_path: str, category: str, data: bytes | None = None) -> bool:
    key = (relative_path, category)
    if key in _EXEMPTIONS:
        return True
    reviewed = _REVIEWED_EXEMPTIONS.get(key)
    return reviewed is not None and data is not None and hashlib.sha256(data).hexdigest() == reviewed[0]


def _line_number(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _assignment_value(match: re.Match[bytes]) -> bytes:
    raw_value = match.group("value").strip()
    quote = raw_value[:1]
    if quote in {b"'", b'"'}:
        value = bytearray()
        escaped = False
        for byte in raw_value[1:]:
            if escaped:
                if byte not in {quote[0], ord("\\")}:
                    value.append(ord("\\"))
                value.append(byte)
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == quote[0]:
                return bytes(value)
            else:
                value.append(byte)
        raw_value = bytes(value)

    inline_comment = re.search(rb"[ \t]+#", raw_value)
    if inline_comment:
        raw_value = raw_value[: inline_comment.start()]
    return raw_value.strip()


def _content_path_redactions(data: bytes) -> tuple[bytes, ...]:
    values: set[bytes] = set()
    for pattern in _CREDENTIAL_PATTERNS:
        values.update(match.group(0) for match in pattern.finditer(data))
    for match in _SECRET_ASSIGNMENT_PATTERN.finditer(data):
        values.add(_assignment_value(match))
    for pattern in _USER_PATH_PATTERNS:
        values.update(match.group("owner") for match in pattern.finditer(data))
    values.update(match.group(0) for match in _PRIVATE_KEY_PATTERN.finditer(data))
    return tuple(values)


def _has_binary_content(data: bytes) -> bool:
    if any(data.startswith(signature) for signature in _BINARY_SIGNATURES):
        return True
    sample = data[:8192]
    if b"\x00" in sample:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _is_provenance_binary(path: PurePosixPath) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    return (
        path.suffix.lower() in _PROVENANCE_BINARY_SUFFIXES
        and ("models" in parts or "icons" in parts)
    )


def _is_internal_report(path: PurePosixPath) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    private_area = any(part.lstrip(".") == "superpowers" for part in parts) or any(
        "workflow" in part and ("private" in part or "internal" in part)
        for part in parts
    )
    report_name = any(
        word in path.name.lower()
        for word in (
            "audit",
            "calibration",
            "handoff",
            "handover",
            "inspection",
            "internal-report",
            "private-report",
        )
    )
    return (
        "reports" in parts
        or private_area
        or (path.suffix.lower() in {".csv", ".json", ".md", ".txt"} and report_name)
    )


def _scan_content(
    relative_path: str,
    data: bytes,
    path_redactions: tuple[bytes, ...],
) -> list[Finding]:
    findings: list[Finding] = []
    rules: tuple[tuple[str, re.Pattern[bytes], str], ...] = (
        ("credential-format", re.compile(b"|".join(p.pattern for p in _CREDENTIAL_PATTERNS)), "blocking"),
        ("private-key", _PRIVATE_KEY_PATTERN, "blocking"),
        ("secret-assignment", _SECRET_ASSIGNMENT_PATTERN, "blocking"),
    )
    for category, pattern, severity in rules:
        if _is_exempt(relative_path, category, data):
            continue
        for match in pattern.finditer(data):
            findings.append(
                _finding(
                    category,
                    relative_path,
                    severity,
                    _line_number(data, match.start()),
                    path_redactions=path_redactions,
                )
            )

    if not _is_exempt(relative_path, "user-path", data):
        for pattern in _USER_PATH_PATTERNS:
            for match in pattern.finditer(data):
                findings.append(
                    _finding(
                        "user-path",
                        relative_path,
                        "blocking",
                        _line_number(data, match.start()),
                        path_redactions=path_redactions,
                    )
                )
    return findings


def _iter_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        base = Path(current)
        traversed: list[str] = []
        for directory in sorted(directories):
            candidate = base / directory
            if candidate.is_symlink():
                paths.append(candidate)
            elif directory == ".git":
                continue
            elif directory in _EXCLUDED_DIRECTORIES:
                paths.append(candidate)
            else:
                traversed.append(directory)
        directories[:] = traversed
        paths.extend(base / filename for filename in sorted(filenames))
    return paths


def audit_tree(root: Path) -> list[Finding]:
    """Return redacted findings for *root* without modifying or escaping it."""

    root = Path(root).resolve()
    if not root.is_dir():
        return [_finding("invalid-root", ".", "blocking")]

    findings: list[Finding] = []
    for path in _iter_paths(root):
        try:
            relative_path = path.relative_to(root).as_posix()
        except ValueError:
            continue

        if path.is_symlink():
            try:
                path.resolve(strict=False).relative_to(root)
            except (OSError, RuntimeError, ValueError):
                findings.append(_finding("symlink-outside-root", relative_path, "blocking"))
                continue
        if path.is_dir():
            if path.name in _EXCLUDED_DIRECTORIES:
                findings.append(_finding("excluded-location", relative_path, "blocking"))
            continue

        relative = PurePosixPath(relative_path)
        is_internal_report = _is_internal_report(relative) and not _is_exempt(
            relative_path, "internal-report"
        )

        try:
            data = path.read_bytes()
        except (OSError, RuntimeError):
            if is_internal_report:
                findings.append(_finding("internal-report", relative_path, "blocking"))
            findings.append(_finding("unreadable-file", relative_path, "blocking"))
            continue

        path_redactions = _content_path_redactions(data)
        if is_internal_report:
            findings.append(
                _finding(
                    "internal-report",
                    relative_path,
                    "blocking",
                    path_redactions=path_redactions,
                )
            )
        findings.extend(_scan_content(relative_path, data, path_redactions))
        if _has_binary_content(data):
            category = (
                "provenance-binary-review"
                if _is_provenance_binary(relative)
                else "unexpected-binary"
            )
            severity = "review" if category == "provenance-binary-review" else "blocking"
            findings.append(
                _finding(
                    category,
                    relative_path,
                    severity,
                    path_redactions=path_redactions,
                )
            )

    return sorted(
        findings,
        key=lambda finding: (
            str(finding["path"]),
            int(finding.get("line", 0)),
            str(finding["category"]),
        ),
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    arguments = parser.parse_args()

    findings = audit_tree(arguments.root)
    severities = Counter(str(finding["severity"]) for finding in findings)
    categories = Counter(str(finding["category"]) for finding in findings)
    print(
        json.dumps(
            {
                "finding_count": len(findings),
                "severity_counts": dict(sorted(severities.items())),
                "category_counts": dict(sorted(categories.items())),
            },
            sort_keys=True,
        )
    )
    return 1 if severities["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(_main())
