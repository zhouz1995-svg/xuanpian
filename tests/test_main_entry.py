"""Entry-point bootstrap regression coverage."""

from __future__ import annotations

import multiprocessing
import runpy
from pathlib import Path

import pytest

from src import main as app_entry


class _StartupStopped(RuntimeError):
    pass


def test_main_dispatches_frozen_multiprocessing_before_gui_startup(
    monkeypatch,
) -> None:
    events: list[str] = []

    monkeypatch.setattr(
        multiprocessing,
        "freeze_support",
        lambda: events.append("freeze_support"),
    )

    def stop_before_gui(_argv) -> None:
        events.append("QApplication")
        raise _StartupStopped

    monkeypatch.setattr(app_entry, "QApplication", stop_before_gui)

    with pytest.raises(_StartupStopped):
        app_entry.main()

    assert events == ["freeze_support", "QApplication"]


def test_importing_entry_module_does_not_dispatch_or_start_gui(
    monkeypatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        multiprocessing,
        "freeze_support",
        lambda: events.append("freeze_support"),
    )
    entry_path = Path(app_entry.__file__)

    runpy.run_path(str(entry_path), run_name="src.main_import_probe")

    assert events == []
