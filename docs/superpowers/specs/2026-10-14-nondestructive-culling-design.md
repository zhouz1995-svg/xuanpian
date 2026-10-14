# XuanPian 0.1.1 Non-Destructive Culling Design

## Objective

Make culling safe and predictable for first-time users: assigning 保留、待定、舍弃 records a project decision without moving or deleting source files. File-system changes happen only in the existing explicit export workflow.

This is the first implementation phase from the product review. It also fixes the adjacent UI defects that currently make the safety model unclear: a permanently disabled batch-mark control, a list toggle that does not visibly produce the compact list in the packaged app, an enabled export action for an empty project, and English standard buttons inside the Chinese-first interface.

## Product decision

Three approaches were considered:

1. Keep moving files when each photo is marked. This preserves the current implementation but creates avoidable risk and makes users reluctant to begin culling.
2. Store marks non-destructively and perform copy or move only through export. This is the selected approach because it matches established photo-review expectations and reuses the existing project store and export service.
3. Create a separate managed working library and move imported files into it first. This would offer stronger catalog semantics but adds storage, migration, and recovery complexity that is inappropriate for 0.1.1.

## Scope

### Non-destructive marking

- `PhotoMark.KEEP`, `PhotoMark.HOLD`, and `PhotoMark.DISCARD` update the in-memory model and the active project database only.
- Marking never calls `PhotoMoveService`, never creates mark-status directories, and never changes JPEG or RAW source paths.
- JPEG/RAW pairing remains intact in project state and is still exported as one unit.
- The culling workspace continues to advance after a mark, supports status and analysis filters, shows completion counts, and permits review of 待定 and 舍弃 results.
- Undo restores the previous mark in the model and database without file-system work.
- The warning that marking moves original files is removed. A short first-entry notice may explain that marking is non-destructive, but entering culling must not require a destructive-action confirmation.

### Batch marking

- The toolbar batch-mark button is enabled when the project is available, no blocking job is active, and at least one visible photo is selected.
- Its 保留、待定、舍弃 actions apply one mark to all selected photo IDs in a single project-store write.
- A persistence failure leaves all affected in-memory marks unchanged and reports a user-facing error.
- Batch marking does not enter the culling workspace and does not touch source files.

### Explicit file operations

- Export remains the only normal workflow that can copy or move source files.
- Copy remains the default mode.
- Selecting move mode requires an explicit Chinese confirmation after the destination and item count are known.
- Selecting overwrite conflict handling requires an explicit Chinese confirmation after the destination and item count are known.
- The export dialog shows the selected range count and a filename example before the destination picker is opened.
- A successful move export updates the project consistently using the existing export completion path; interrupted exports continue to use existing recovery behavior.

### Browse-view correctness

- 网格 displays thumbnail cards.
- 列表 displays compact 56-pixel rows using `ThumbnailListDelegate`, including thumbnail, filename, conclusions, capture time, and mark.
- Switching views preserves selection, filters, and scrolls the selected photo into view.
- Regression coverage must exercise the same public `MainWindow.set_view_mode()` route used by the packaged application.

### Empty and localized states

- Export is disabled when the active project has no photos and becomes enabled once at least one photo is available.
- The empty state visually prioritizes 导入照片 and provides a direct import action.
- Dialog buttons owned by the application use Chinese labels such as 继续、取消、确定. The implementation must not rely on platform translations for safety-critical prompts.
- The thumbnail-size slider has an accessible name and tooltip that identify it as 缩略图大小.
- The README must no longer claim five-star-style “评分” until a rating system exists; it should describe the implemented marks accurately.

## Data flow

1. The user imports or reopens a folder-backed project.
2. Analysis and preview continue to operate on the original paths.
3. A single-photo or batch mark is validated against the active project and persisted through `ProjectStore.save_marks`.
4. Only after persistence succeeds are the corresponding `ImageFileModel.mark` values and UI-derived counts updated.
5. Culling navigation advances locally; no move coordinator is started.
6. When the user explicitly exports, `ExportService` performs copy or move according to the confirmed request.

## Error handling

- Database write failure: keep the previous marks in memory, keep the current selection/session position, and display a concise Chinese message.
- Missing project or stale photo ID: perform no mutation and show a status message rather than raising into the UI event loop.
- Export move or overwrite: cancel safely if the user declines the Chinese confirmation.
- View switching: if the current filter is empty, preserve the requested view mode so it is applied when photos reappear.

## Testing strategy

- Add failing tests before each production change.
- Unit-test non-destructive single and batch marking, including database failure rollback.
- Update culling end-to-end tests to assert that source paths and directory contents do not change after marks and undo.
- Add a main-window regression test proving grid/list visibility and compact list geometry through `set_view_mode`.
- Add export tests for move and overwrite confirmation, item-count text, and Chinese buttons.
- Add accessibility and empty-state tests for the export button and thumbnail-size slider.
- Run the complete off-screen pytest suite after focused tests pass.
- Rebuild and inspect the arm64 application only after source-level tests pass.

## Compatibility and migration

- Existing project databases and stored marks remain readable; no schema migration is required.
- Existing status directories created by 0.1.0 are not automatically moved back. Reopening continues to reconcile their recorded current paths.
- Existing export copy/move behavior and conflict policies remain available.
- No new runtime dependency is introduced.

## Non-goals for this phase

- Five-star ratings, color labels, XMP sidecars, Lightroom/Capture One integration.
- Two-up or four-up synchronized comparison.
- New analysis algorithms, camera mappings, or confidence visualization.
- Recent-project UI, preferences, update checks, notarization, or additional platforms.

Those items remain in subsequent phases so this safety change can ship and be validated independently.
