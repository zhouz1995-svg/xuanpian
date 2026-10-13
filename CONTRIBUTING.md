# Contributing

This is a source candidate. Before submitting a patch, read [the project license](LICENSE), [third-party release blockers](THIRD_PARTY_NOTICES.md), and [privacy guidance](PRIVACY.md). Do not add real photos, private filenames/paths, EXIF, databases, logs, generated reports, credentials, or signing material to a commit or issue. Prefer synthetic fixtures.

Use Python 3.11 on an arm64 Mac. From the repository root, create an environment and install the pinned runtime, build, and test dependencies:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r packaging/requirements-macos-arm64.lock
.venv/bin/python -m pip check
```

Run tests from the repository root so the local project source is importable without an unpinned project install. The lock includes the `setuptools` build backend used by `scripts/build_wheel.py` with build isolation disabled; it is not a list of everything shipped in the `.app`. For an arm64 release build, follow the exact workflow in [macOS build instructions](docs/building-macos.md).

Run tests from the repository root:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
```

For documentation changes, run `QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/test_public_documents.py -q` as well. Do not change the accepted AF camera mapping, focus thresholds, or classification behavior as a side effect of documentation work. If a change alters analysis behavior, include focused regression evidence and state the intended user-facing difference.

Public author attribution and a public contact channel have not yet been selected. Do not copy private Git identities into new public commits or invent a support address. A maintainer will set the public contribution workflow before opening a remote repository.
