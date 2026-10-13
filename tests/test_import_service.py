from pathlib import Path
from itertools import permutations

import pytest

from src.xuanpian.services import import_service
from src.xuanpian.services.import_service import (
    ImportService,
    MANAGED_STATUS_DIRECTORIES,
    scan_images,
)


def _flatten(batches):
    return [item for batch in batches for item in batch]


def _latest(snapshots):
    latest = {}
    for item in snapshots:
        for photo_id in getattr(item, "superseded_photo_ids", ()):
            latest.pop(photo_id, None)
        latest[item.photo_id] = item
    return latest


def test_jpeg_and_raw_upsert_same_logical_photo_in_either_order(tmp_path) -> None:
    jpeg = tmp_path / "DSC_1.JPG"
    raw = tmp_path / "DSC_1.NEF"
    jpeg.touch()
    raw.touch()

    forward = _flatten(
        ImportService(path_source=lambda root: iter((jpeg, raw))).scan(tmp_path, 1)
    )
    reverse = _flatten(
        ImportService(path_source=lambda root: iter((raw, jpeg))).scan(tmp_path, 1)
    )

    expected = (str(jpeg.resolve()), str(raw.resolve()))
    assert forward[-1].source_paths == expected
    assert reverse[-1].source_paths == expected
    assert forward[0].photo_id == forward[-1].photo_id
    assert reverse[0].photo_id == reverse[-1].photo_id


def test_same_stem_in_different_directories_never_pairs(tmp_path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    jpeg = left / "DSC_1.JPG"
    raw = right / "DSC_1.NEF"
    jpeg.touch()
    raw.touch()

    snapshots = _flatten(ImportService().scan(tmp_path, 1))

    assert len({item.photo_id for item in snapshots}) == 2
    assert all(len(item.source_paths) == 1 for item in snapshots)


def test_first_batch_does_not_exhaust_source(tmp_path) -> None:
    consumed: list[int] = []
    paths = [tmp_path / f"{index}.jpg" for index in range(3)]
    for path in paths:
        path.touch()

    def source(root: Path):
        for index, path in enumerate(paths):
            consumed.append(index)
            yield path

    batches = ImportService(path_source=source).scan(tmp_path, batch_size=1)

    assert next(batches)[0].display_name == "0.jpg"
    assert consumed == [0]


def test_related_camera_suffix_pairs_only_when_unambiguous(tmp_path) -> None:
    raw = tmp_path / "DSC_2.NEF"
    jpeg = tmp_path / "DSC_2_01.JPG"
    raw.touch()
    jpeg.touch()

    snapshots = _flatten(
        ImportService(path_source=lambda root: iter((jpeg, raw))).scan(tmp_path, 1)
    )

    latest = _latest(snapshots)
    assert len(latest) == 1
    assert next(iter(latest.values())).source_paths == (
        str(jpeg.resolve()),
        str(raw.resolve()),
    )


def test_ambiguous_related_jpegs_remain_separate(tmp_path) -> None:
    raw = tmp_path / "DSC_3.NEF"
    first = tmp_path / "DSC_3_01.JPG"
    second = tmp_path / "DSC_3_02.JPG"
    for path in (raw, first, second):
        path.touch()

    snapshots = _flatten(
        ImportService(
            path_source=lambda root: iter((raw, first, second))
        ).scan(tmp_path, 1)
    )

    latest = _latest(snapshots)
    assert len(latest) == 3
    assert all(len(item.source_paths) == 1 for item in latest.values())


def test_hidden_and_unsupported_files_are_ignored(tmp_path) -> None:
    visible = tmp_path / "photo.jpg"
    hidden = tmp_path / "._photo.jpg"
    text = tmp_path / "notes.txt"
    for path in (visible, hidden, text):
        path.touch()

    snapshots = _flatten(ImportService().scan(tmp_path, 10))

    assert [item.display_name for item in snapshots] == ["photo.jpg"]


def test_legacy_scan_returns_one_jpeg_preferred_path_per_photo(tmp_path) -> None:
    jpeg = tmp_path / "DSC_4.JPG"
    raw = tmp_path / "DSC_4.NEF"
    jpeg.touch()
    raw.touch()

    paths = scan_images(str(tmp_path))

    assert paths == [str(jpeg.resolve())]


@pytest.mark.parametrize(
    "ordered_names",
    [
        ("DSC_5.NEF", "DSC_5_01.NEF", "DSC_5_01.JPG"),
        ("DSC_5_01.JPG", "DSC_5_01.NEF", "DSC_5.NEF"),
    ],
)
def test_exact_pair_takes_precedence_over_related_pair(
    tmp_path, ordered_names
) -> None:
    paths = {name: tmp_path / name for name in ordered_names}
    for path in paths.values():
        path.touch()

    snapshots = _flatten(
        ImportService(
            path_source=lambda root: (paths[name] for name in ordered_names)
        ).scan(tmp_path, 1)
    )
    latest = _latest(snapshots)

    assert len(latest) == 2
    assert {
        item.source_paths for item in latest.values()
    } == {
        (str(paths["DSC_5.NEF"].resolve()),),
        (
            str(paths["DSC_5_01.JPG"].resolve()),
            str(paths["DSC_5_01.NEF"].resolve()),
        ),
    }


@pytest.mark.parametrize(
    "names",
    [
        ("A.JPG", "A.NEF", "A.CR2"),
        ("B.JPG", "B.JPEG", "B.NEF"),
    ],
)
def test_exact_stem_multiplicity_is_never_paired_regardless_of_order(
    tmp_path, names
) -> None:
    paths = {name: tmp_path / name for name in names}
    for path in paths.values():
        path.touch()

    for ordered_names in permutations(names):
        snapshots = _flatten(
            ImportService(
                path_source=lambda root, order=ordered_names: (
                    paths[name] for name in order
                )
            ).scan(tmp_path, 1)
        )
        latest = _latest(snapshots)

        assert len(latest) == 3
        assert all(
            len(candidate.source_paths) == 1
            for candidate in latest.values()
        )


@pytest.mark.parametrize(
    ("jpeg_name", "raw_name"),
    [
        ("IMG_1_v2.JPG", "IMG_1.NEF"),
        ("IMG_2.JPG", "IMG_2_01.NEF"),
    ],
)
def test_related_stem_pairing_is_symmetric_and_general(
    tmp_path, jpeg_name: str, raw_name: str
) -> None:
    jpeg = tmp_path / jpeg_name
    raw = tmp_path / raw_name
    jpeg.touch()
    raw.touch()

    latest = _latest(_flatten(ImportService().scan(tmp_path, 1)))

    assert len(latest) == 1
    assert next(iter(latest.values())).source_paths == (
        str(jpeg.resolve()),
        str(raw.resolve()),
    )


def test_camera_suffix_jpeg_is_streamed_without_exhausting_source(tmp_path) -> None:
    consumed: list[int] = []
    paths = [tmp_path / "DSC_0001_01.JPG", tmp_path / "later.jpg"]
    for path in paths:
        path.touch()

    def source(root: Path):
        for index, path in enumerate(paths):
            consumed.append(index)
            yield path

    batches = ImportService(path_source=source).scan(tmp_path, 1)

    assert next(batches)[0].display_name == "DSC_0001_01.JPG"
    assert consumed == [0]


def test_unavailable_raw_capability_logs_path(tmp_path, monkeypatch, caplog) -> None:
    raw = tmp_path / "unsupported.NEF"
    raw.touch()
    monkeypatch.setattr(import_service, "RAW_EXTENSIONS", set())

    snapshots = _flatten(ImportService().scan(tmp_path, 1))

    assert snapshots == []
    assert str(raw) in caplog.text
    assert "rawpy" in caplog.text


def test_unavailable_heif_capability_logs_path(
    tmp_path, monkeypatch, caplog
) -> None:
    heif = tmp_path / "unsupported.HEIC"
    heif.touch()
    monkeypatch.setattr(
        import_service,
        "RASTER_EXTENSIONS",
        import_service.RASTER_EXTENSIONS
        - import_service.KNOWN_HEIF_EXTENSIONS,
    )

    snapshots = _flatten(ImportService().scan(tmp_path, 1))

    assert snapshots == []
    assert str(heif) in caplog.text
    assert "pillow-heif" in caplog.text


def test_iter_paths_yields_natural_filename_order(tmp_path) -> None:
    for name in ("IMG_10.jpg", "IMG_1.jpg", "IMG_2.jpg", "a_2.jpg", "a_10.jpg"):
        (tmp_path / name).touch()

    assert [path.name for path in ImportService.iter_paths(tmp_path)] == [
        "a_2.jpg",
        "a_10.jpg",
        "IMG_1.jpg",
        "IMG_2.jpg",
        "IMG_10.jpg",
    ]


def test_scan_images_returns_natural_filename_order(tmp_path) -> None:
    for name in ("IMG_10.jpg", "IMG_1.jpg", "IMG_2.jpg"):
        (tmp_path / name).touch()

    paths = scan_images(str(tmp_path))
    assert [Path(path).name for path in paths] == [
        "IMG_1.jpg",
        "IMG_2.jpg",
        "IMG_10.jpg",
    ]


def test_scanner_prunes_exact_managed_status_directories_at_every_depth(
    tmp_path: Path,
) -> None:
    visible = (
        tmp_path / "root.jpg",
        tmp_path / "nested" / "visible.jpg",
        tmp_path / "保留照片.jpg",
        tmp_path / "保留副本" / "also-visible.jpg",
    )
    for path in visible:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    for directory in MANAGED_STATUS_DIRECTORIES:
        for parent in (tmp_path, tmp_path / "nested" / "deeper"):
            hidden = parent / directory / f"{directory}.jpg"
            hidden.parent.mkdir(parents=True, exist_ok=True)
            hidden.touch()

    paths = set(scan_images(str(tmp_path)))

    assert paths == {str(path.resolve()) for path in visible}


def test_managed_directory_match_is_case_sensitive_and_exact(
    tmp_path: Path,
) -> None:
    exact = tmp_path / "保留" / "managed.jpg"
    near = tmp_path / "保留副本" / "visible.jpg"
    sibling_file = tmp_path / "保留照片.jpg"
    for path in (exact, near, sibling_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    paths = set(scan_images(str(tmp_path)))

    assert str(exact.resolve()) not in paths
    assert paths == {str(near.resolve()), str(sibling_file.resolve())}


def test_scanner_does_not_follow_a_directory_symlink_into_managed_assets(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "保留"
    managed.mkdir()
    (managed / "hidden.jpg").touch()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(managed, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    assert scan_images(str(tmp_path)) == []


@pytest.mark.parametrize("directory", sorted(MANAGED_STATUS_DIRECTORIES))
def test_exact_managed_root_is_never_scanned(
    tmp_path: Path,
    directory: str,
) -> None:
    root = tmp_path / directory
    root.mkdir()
    (root / "hidden.jpg").touch()

    assert list(ImportService.iter_paths(root)) == []
    assert scan_images(str(root)) == []


@pytest.mark.parametrize("directory", sorted(MANAGED_STATUS_DIRECTORIES))
@pytest.mark.parametrize("root_spelling", (".", ".."))
def test_lexical_managed_root_is_never_scanned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
    root_spelling: str,
) -> None:
    managed = tmp_path / directory
    managed.mkdir()
    (managed / "hidden.jpg").touch()
    working_directory = managed
    if root_spelling == "..":
        working_directory = managed / "child"
        working_directory.mkdir()
    monkeypatch.chdir(working_directory)
    root = Path(root_spelling)

    iter_paths_result = list(ImportService.iter_paths(root))
    scan_images_result = scan_images(str(root))

    assert iter_paths_result == []
    assert scan_images_result == []


@pytest.mark.parametrize("target_name", ("ordinary", "保留"))
def test_relative_directory_symlink_root_is_never_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    target = tmp_path / target_name
    target.mkdir()
    (target / "hidden.jpg").touch()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")
    monkeypatch.chdir(tmp_path)
    relative_alias = Path("alias")

    assert list(ImportService.iter_paths(relative_alias)) == []
    assert scan_images(str(relative_alias)) == []


def test_near_name_root_remains_scannable(tmp_path: Path) -> None:
    root = tmp_path / "保留副本"
    root.mkdir()
    photo = root / "保留照片.jpg"
    photo.touch()

    assert list(ImportService.iter_paths(root)) == [photo]
    assert scan_images(str(root)) == [str(photo.resolve())]


@pytest.mark.parametrize("root_spelling", (".", ".."))
def test_lexical_near_name_root_remains_scannable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_spelling: str,
) -> None:
    root_directory = tmp_path / "保留副本"
    root_directory.mkdir()
    photo = root_directory / "visible.jpg"
    photo.touch()
    working_directory = root_directory
    if root_spelling == "..":
        working_directory = root_directory / "child"
        working_directory.mkdir()
    monkeypatch.chdir(working_directory)
    root = Path(root_spelling)

    iter_paths_result = [path.resolve() for path in ImportService.iter_paths(root)]
    scan_images_result = scan_images(str(root))

    assert iter_paths_result == [photo.resolve()]
    assert scan_images_result == [str(photo.resolve())]


@pytest.mark.parametrize("root_kind", ("missing", "file"))
def test_non_directory_root_remains_an_empty_scan(
    tmp_path: Path,
    root_kind: str,
) -> None:
    root = tmp_path / root_kind
    if root_kind == "file":
        root.touch()

    assert list(ImportService.iter_paths(root)) == []
    assert scan_images(str(root)) == []


@pytest.mark.parametrize(
    "target_parent",
    (*sorted(MANAGED_STATUS_DIRECTORIES), "external"),
)
def test_terminal_file_symlink_is_rejected_by_both_public_scanners(
    tmp_path: Path,
    target_parent: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / target_parent / "target.jpg"
    target.parent.mkdir()
    target.write_bytes(b"target")
    alias = root / "alias.jpg"
    try:
        alias.symlink_to(target)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    assert list(ImportService.iter_paths(root)) == []
    assert scan_images(str(root)) == []
    assert alias.is_symlink()
    assert target.read_bytes() == b"target"


def test_terminal_regular_file_remains_visible_to_both_public_scanners(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    photo = root / "ordinary.jpg"
    photo.write_bytes(b"jpeg")

    assert list(ImportService.iter_paths(root)) == [photo]
    assert scan_images(str(root)) == [str(photo.resolve())]
