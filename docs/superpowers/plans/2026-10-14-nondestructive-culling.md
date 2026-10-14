# XuanPian 0.1.1 Non-Destructive Culling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all photo marks non-destructive, reserve file moves for an explicitly confirmed export, and fix the adjacent browse, batch, empty-state, localization, and accessibility defects.

**Architecture:** Reuse `ProjectStore.save_marks()` as the only persistence boundary for single and batch marking, then update in-memory models only after the atomic database write succeeds. Keep the existing move/export recovery services for explicit exports and old project recovery, but remove them from the normal culling path. UI state is derived from project availability, photo count, selection, and pending jobs through small refresh helpers.

**Tech Stack:** Python 3.11, PyQt6, SQLite `ProjectStore`, pytest/pytest-qt, existing export and recovery services.

**Spec:** `docs/superpowers/specs/2026-10-14-nondestructive-culling-design.md`

## Global Constraints

- Existing project databases remain readable; no schema migration is introduced.
- Marking 保留、待定、舍弃 must never change JPEG or RAW paths or call the move coordinator.
- Copy remains the default export mode; move and overwrite require explicit Chinese confirmation.
- No new runtime dependency is introduced.
- Every production behavior change starts with a focused failing test.
- Existing interrupted-move recovery remains available for projects created by 0.1.0.

---

### Task 1: Non-destructive single-photo culling and undo

**Files:**
- Modify: `src/xuanpian/ui/main_window.py`
- Modify: `tests/test_culling_e2e.py`
- Modify: `tests/test_keyboard_manager.py`

**Interfaces:**
- Consumes: `ProjectStore.save_marks(marks: Sequence[tuple[str, str]]) -> None`, `CullingSession.advance_after_mark(photo_id: str) -> None`.
- Produces: `_MarkUndoCommand(photo_id: str, before_mark: PhotoMark, after_mark: PhotoMark)` and non-destructive `MainWindow.request_culling_mark()` / `request_culling_undo()` behavior.

- [ ] **Step 1: Write the failing non-destructive mark test**

Replace the move-oriented first acceptance test in `tests/test_culling_e2e.py` with:

```python
def test_real_product_flow_marks_without_moving_undoes_finishes_and_reopens(
    qapp,
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    original_paths = {
        photo_id: tuple(asset.fingerprint.path for asset in record.source_assets)
        for photo_id, record in project.records.items()
    }
    window = _window(project)
    try:
        window.enter_culling()
        window.request_culling_mark("pair", PhotoMark.KEEP)
        kept = project.store.get_photo("pair")
        assert kept is not None
        assert kept.mark == PhotoMark.KEEP.value
        assert tuple(asset.fingerprint.path for asset in kept.source_assets) == original_paths["pair"]
        assert project.store.list_pending_photo_moves() == ()
        window.request_culling_undo()
        assert project.store.get_photo("pair").mark == PhotoMark.UNPROCESSED.value
        assert tuple(asset.fingerprint.path for asset in project.store.get_photo("pair").source_assets) == original_paths["pair"]
    finally:
        window.close()
        qapp.processEvents()
```

- [ ] **Step 2: Run the test and verify RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_culling_e2e.py::test_real_product_flow_marks_without_moving_undoes_finishes_and_reopens -v`

Expected: FAIL because the current mark path starts `PhotoMoveService` and changes the source location.

- [ ] **Step 3: Implement database-first single marking**

Add a small undo value object:

```python
@dataclass(frozen=True, slots=True)
class _MarkUndoCommand:
    photo_id: str
    before_mark: PhotoMark
    after_mark: PhotoMark
```

Change `request_culling_mark()` to validate the current photo, call `store.save_marks(((photo_id, mark.value),))`, update `model.mark` only on success, refresh the affected model/counts, store `_MarkUndoCommand`, advance the culling session, and show `照片标记已保存；原文件未移动`.

- [ ] **Step 4: Run the focused mark test and verify GREEN**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_culling_e2e.py::test_real_product_flow_marks_without_moving_undoes_finishes_and_reopens -v`

Expected: PASS.

- [ ] **Step 5: Write failing undo and persistence-failure tests**

Add an `_FailingMarkStore` wrapper that delegates reads to the real project store and raises `OSError("mark database unavailable")` from `save_marks()`, then add:

```python
def test_culling_mark_store_failure_keeps_model_session_and_files(
    qapp,
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    window = _window(project)
    source_paths = tuple(project.models["pair"].source_paths)
    window.enter_culling()
    current = window._culling_session.current_id
    window._project_store = _FailingMarkStore(project.store)
    window.request_culling_mark(current, PhotoMark.KEEP)
    assert project.models["pair"].mark == PhotoMark.UNPROCESSED.value
    assert window._culling_session.current_id == current
    assert tuple(project.models["pair"].source_paths) == source_paths
    assert all(Path(path).exists() for path in source_paths)
```

- [ ] **Step 6: Run the new tests and verify RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_culling_e2e.py -k "real_product_flow or store_failure" -v`

Expected: undo still depends on the reverse move pipeline or failure changes state.

- [ ] **Step 7: Implement database-first undo and remove the destructive entry warning**

Make `request_culling_undo()` atomically restore the prior mark, refresh the model/session, then clear `_MarkUndoCommand`. Remove `_confirm_culling_notice()` from `enter_culling()` and keep existing pending-move recovery code only for legacy recovery.

- [ ] **Step 8: Run focused culling and keyboard tests**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_culling_e2e.py tests/test_keyboard_manager.py -q`

Expected: PASS with no culling mark test expecting a file move.

- [ ] **Step 9: Commit**

```bash
git add src/xuanpian/ui/main_window.py tests/test_culling_e2e.py tests/test_keyboard_manager.py
git commit -m "feat: make culling marks non-destructive"
```

### Task 2: Functional atomic batch marking

**Files:**
- Modify: `src/xuanpian/ui/main_window.py`
- Modify: `tests/test_main_window_jobs.py`
- Modify: `tests/test_thumbnail_browser_cards.py`

**Interfaces:**
- Consumes: `MainWindow.mark_photos(photo_ids, mark) -> bool` from the existing database-first path.
- Produces: selection-derived `_update_batch_mark_state()` and connected 保留、待定、舍弃 menu actions.

- [ ] **Step 1: Write failing batch-control tests**

```python
def test_batch_mark_enables_for_selection_and_persists_atomically(qapp) -> None:
    window = MainWindow()
    first = ImageFileModel("/A.JPG", photo_id="p1")
    second = ImageFileModel("/B.JPG", photo_id="p2")
    store = _RecordingStore()
    window._project_store = store
    window._active_project_id = uuid4()
    window.thumbnail_browser.set_models([first, second])
    window.select_all()
    window._update_batch_mark_state()
    assert window._batch_mark_btn.isEnabled()
    window._on_batch_mark(PhotoMark.KEEP.value)
    assert [first.mark, second.mark] == [PhotoMark.KEEP.value] * 2
    assert store.saved_marks == [(("p1", "保留"), ("p2", "保留"))]
```

Retain and extend the existing `test_batch_mark_failure_keeps_every_model_unchanged` to assert `mark_photos()` returns `False` and both models keep their previous marks.

- [ ] **Step 2: Run the tests and verify RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_main_window_jobs.py -k "batch_mark" -v`

Expected: the toolbar button and actions remain disabled.

- [ ] **Step 3: Connect actions and derive enabled state**

Create each menu action with its `PhotoMark.value`, connect it to `_on_batch_mark`, and enable the button only when the selection is non-empty, the project is available, no import/export/move is pending, and recovery is not blocked. Keep `mark_photos()` database-first so a failed `save_marks()` updates no model.

- [ ] **Step 4: Run batch and selection tests**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_main_window_jobs.py tests/test_thumbnail_browser_cards.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xuanpian/ui/main_window.py tests/test_main_window_jobs.py tests/test_thumbnail_browser_cards.py
git commit -m "feat: enable atomic batch marking"
```

### Task 3: Browse-view, empty-state, localization, and accessibility fixes

**Files:**
- Modify: `src/xuanpian/ui/thumbnail_browser.py`
- Modify: `src/xuanpian/ui/main_window.py`
- Modify: `src/xuanpian/ui/export_dialog.py`
- Modify: `tests/test_virtualized_ui_acceptance.py`
- Modify: `tests/test_status_bar.py`
- Modify: `tests/test_export.py`

**Interfaces:**
- Consumes: `ThumbnailBrowser.set_view_mode(mode: str) -> None` and the shared selection model.
- Produces: `ThumbnailBrowser.import_requested`, `_refresh_export_enabled_state()`, and Chinese application-owned dialog buttons.

- [ ] **Step 1: Write failing public-route view regression test**

```python
def test_main_window_list_mode_shows_only_compact_list(qapp):
    window = MainWindow()
    window._thumbnail_browser.set_models(_models()[:3])
    window.set_view_mode("list")
    assert window._thumbnail_browser._list_view.isVisible()
    assert not window._thumbnail_browser._view.isVisible()
    assert window._thumbnail_browser._list_delegate.sizeHint(option, index).height() == 56
```

- [ ] **Step 2: Run the view test and verify RED against the packaged-route defect**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_virtualized_ui_acceptance.py -k "list_mode" -v`

Expected: FAIL until the public route reliably applies visibility after model/empty-state transitions.

- [ ] **Step 3: Make view state authoritative**

Ensure `_show_grid()` only means “show photo content,” then always delegates actual widget visibility to `_apply_view_visibility()`. Preserve `_view_mode` through empty and filtered-empty transitions and call `updateGeometry()`/viewport updates after switching so the packaged Qt build repaints the correct delegate.

- [ ] **Step 4: Write failing empty-state and accessibility tests**

```python
def test_empty_project_disables_export_and_offers_import(qapp) -> None:
    window = MainWindow()
    window.show()
    assert not window._export_btn.isEnabled()
    assert window._thumbnail_browser._empty_import_btn.isVisible()

def test_thumbnail_slider_names_its_purpose(qapp) -> None:
    window = MainWindow()
    assert window._size_slider.accessibleName() == "缩略图大小"
    assert "缩略图" in window._size_slider.toolTip()
```

- [ ] **Step 5: Run the tests and verify RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_status_bar.py -k "empty_project or thumbnail_slider" -v`

Expected: export is enabled and the slider is unnamed.

- [ ] **Step 6: Implement empty import action, export state, and accessibility**

Add an empty-state `导入照片` button that emits `import_requested`; connect it to `MainWindow._on_import`. Add `_refresh_export_enabled_state()` and call it after model-set changes and export completion. Set the slider accessible name and tooltip explicitly.

- [ ] **Step 7: Write failing Chinese-button tests and implement explicit labels**

Assert `ExportDialog` exposes buttons labelled `导出` and `取消`. Construct those buttons explicitly through `QDialogButtonBox.addButton()` so platform locale cannot replace their text.

- [ ] **Step 8: Run the focused UI tests**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_virtualized_ui_acceptance.py tests/test_status_bar.py tests/test_export.py -q`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/xuanpian/ui/thumbnail_browser.py src/xuanpian/ui/main_window.py src/xuanpian/ui/export_dialog.py tests/test_virtualized_ui_acceptance.py tests/test_status_bar.py tests/test_export.py
git commit -m "fix: clarify browse and empty states"
```

### Task 4: Export preview and destructive-mode confirmation

**Files:**
- Modify: `src/xuanpian/ui/export_dialog.py`
- Modify: `src/xuanpian/ui/main_window.py`
- Modify: `tests/test_export.py`
- Modify: `tests/test_main_window_export.py`
- Modify: `tests/test_async_export.py`

**Interfaces:**
- Consumes: `ExportDialog.selected_group`, `copy_mode`, `conflict_policy`, `prefix`, and `start_number`.
- Produces: `ExportDialog.selected_count: int`, `ExportDialog.filename_example: str`, and `MainWindow._confirm_destructive_export(mode, conflict, count, destination) -> bool`.

- [ ] **Step 1: Write failing dialog-preview tests**

```python
def test_export_dialog_updates_count_and_filename_example(qapp):
    dialog = ExportDialog({"all": ("全部", 8), "sharp": ("清晰", 3)})
    dialog._group_combo.setCurrentIndex(1)
    dialog._prefix_edit.setText("精选")
    dialog._start_num.setValue(12)
    assert dialog.selected_count == 3
    assert dialog.filename_example == "精选_012.jpg"
    assert "3 张" in dialog._selection_summary.text()
```

- [ ] **Step 2: Run the preview test and verify RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_export.py -k "count_and_filename_example" -v`

Expected: the summary fields do not exist.

- [ ] **Step 3: Implement reactive count and filename preview**

Store group counts by key, update the summary when the group changes, and update the example when prefix/start number changes. For an empty prefix show `保持原文件名，例如 IMG_0001.jpg`.

- [ ] **Step 4: Write failing move/overwrite confirmation tests**

```python
@pytest.mark.parametrize(
    ("copy_mode", "conflict"),
    ((False, ConflictPolicy.UNIQUE), (True, ConflictPolicy.OVERWRITE)),
)
def test_destructive_export_decline_stops_before_service(
    qapp,
    monkeypatch,
    tmp_path: Path,
    copy_mode: bool,
    conflict: ConflictPolicy,
) -> None:
    service = _FakeExportService(ExportReport(results=(), canceled=False))
    window = MainWindow(export_service=service)
    window._thumbnail_browser.set_models(
        [ImageFileModel(str(tmp_path / "first.jpg"), photo_id="p1")]
    )
    monkeypatch.setattr(main_window, "ExportDialog", accepted_dialog(copy_mode, conflict))
    monkeypatch.setattr(main_window.QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path / "out"))
    monkeypatch.setattr(window, "_confirm_destructive_export", lambda *_args: False)
    window._on_export()
    assert service.requests == []
```

Define `accepted_dialog(copy_mode, conflict)` in the same test module as a local factory returning a subclass of the existing `_AcceptedMoveDialog`, so the test exercises both risk modes without UI automation.

- [ ] **Step 5: Run confirmation tests and verify RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_main_window_export.py -k "confirmation" -v`

Expected: export starts without the new confirmation.

- [ ] **Step 6: Implement the confirmation boundary**

After range filtering and destination selection, call `_confirm_destructive_export()`. Use an application-owned `QMessageBox` with `继续` and `取消`; include operation, item count, destination, and overwrite warning. Return immediately if declined.

- [ ] **Step 7: Run export tests**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_export.py tests/test_main_window_export.py tests/test_async_export.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/xuanpian/ui/export_dialog.py src/xuanpian/ui/main_window.py tests/test_export.py tests/test_main_window_export.py tests/test_async_export.py
git commit -m "feat: preview and confirm destructive exports"
```

### Task 5: Documentation, regression suite, and packaged acceptance

**Files:**
- Modify: `README.md`
- Modify: `docs/installation.md`
- Modify: `docs/verification/public-release.md`
- Modify: `tests/test_public_documents.py`
- Modify: `tests/test_packaging_metadata.py`

**Interfaces:**
- Consumes: completed behavior from Tasks 1–4.
- Produces: accurate Chinese-first public documentation and release verification evidence.

- [ ] **Step 1: Write failing documentation assertions**

```python
def test_readme_describes_non_destructive_marks_and_not_unimplemented_ratings():
    text = README.read_text(encoding="utf-8")
    assert "标记不会移动原始文件" in text
    assert "快捷键选片、评分、标记" not in text
```

- [ ] **Step 2: Run documentation tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_public_documents.py -v`

Expected: README still describes ratings and the previous move workflow.

- [ ] **Step 3: Update Chinese and English documentation**

Document that marks are project metadata, file moves occur only during explicit export, move/overwrite are confirmed, and five-star/XMP support is not yet present. Keep the existing beta, platform, AF-metadata, and notarization limitations unchanged.

- [ ] **Step 4: Run documentation and focused feature suites**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_public_documents.py tests/test_culling_e2e.py tests/test_main_window_jobs.py tests/test_virtualized_ui_acceptance.py tests/test_export.py tests/test_main_window_export.py tests/test_async_export.py -q`

Expected: PASS.

- [ ] **Step 5: Run the complete source suite**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`

Expected: all tests pass with no warnings or errors attributable to this change.

- [ ] **Step 6: Run static release checks**

Run: `git diff --check && .venv/bin/python -m pytest tests/test_packaging_metadata.py tests/test_macos_bundle.py -q`

Expected: PASS.

- [ ] **Step 7: Build and inspect the arm64 application**

Run the documented isolated macOS build and bundle verification commands from `docs/building-macos.md`, then manually verify: empty export disabled, list rows compact, batch marks do not move files, undo restores only the mark, move export requires Chinese confirmation, and reopening preserves marks.

- [ ] **Step 8: Commit**

```bash
git add README.md docs/installation.md docs/verification/public-release.md tests/test_public_documents.py tests/test_packaging_metadata.py
git commit -m "docs: document safe culling workflow"
```

- [ ] **Step 9: Record final evidence**

Capture the final commit range, full pytest result, bundle verification result, and remaining deferred phases in the release handoff.
