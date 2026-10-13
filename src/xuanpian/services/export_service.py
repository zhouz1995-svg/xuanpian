"""Transactional file export service."""

from __future__ import annotations

import errno
import os
import shutil
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from uuid import uuid4


class ExportMode(str, Enum):
    COPY = "copy"
    MOVE = "move"


class ConflictPolicy(str, Enum):
    SKIP = "skip"
    UNIQUE = "unique"
    OVERWRITE = "overwrite"


@dataclass(frozen=True)
class ExportItem:
    source: Path
    target_name: str


@dataclass(frozen=True)
class ExportUnit:
    unit_id: str
    items: tuple[ExportItem, ...]


@dataclass(frozen=True)
class ExportRequest:
    destination: Path
    items: tuple[ExportItem, ...]
    mode: ExportMode
    conflict: ConflictPolicy = ConflictPolicy.SKIP
    units: tuple[ExportUnit, ...] = ()


@dataclass(frozen=True)
class ExportItemResult:
    source: Path
    target: Path | None
    success: bool
    error: str | None
    recovery_path: Path | None = None


@dataclass(frozen=True)
class ExportUnitResult:
    unit_id: str
    results: tuple[ExportItemResult, ...]
    success: bool
    error: str | None = None
    recovery_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ExportReport:
    results: tuple[ExportItemResult, ...]
    canceled: bool
    stopped_reason: str | None = None
    unit_results: tuple[ExportUnitResult, ...] = ()


@dataclass(frozen=True)
class _PlannedItem:
    item: ExportItem
    target: Path | None
    error: Exception | None = None


@dataclass
class _UnitMemberState:
    plan: _PlannedItem
    partial: Path | None = None
    backup: Path | None = None
    quarantine: Path | None = None
    promoted: bool = False


class ExportVerificationError(OSError):
    """Raised when a copied file does not match the source metadata."""


class ExportTargetError(OSError):
    """Raised when an export target violates a batch safety boundary."""


class ExportRequestError(OSError):
    """Raised when an export request contains an unsupported option."""


class ExportCleanupError(OSError):
    """Raised when a retained temporary file needs manual cleanup."""

    def __init__(self, message: str, recovery_path: Path) -> None:
        super().__init__(message)
        self.recovery_path = recovery_path


class ExportPromotionConflictError(FileExistsError):
    """Raised when a no-clobber target is claimed during promotion."""


class ExportQuarantineError(OSError):
    """Raised when a quarantined MOVE source must be recovered manually."""

    def __init__(self, message: str, recovery_path: Path) -> None:
        super().__init__(message)
        self.recovery_path = recovery_path


class ExportCanceledError(OSError):
    """Raised when cancellation is observed after MOVE quarantine begins."""


class ExportService:
    def export(
        self, request: ExportRequest, is_canceled: Callable[[], bool]
    ) -> ExportReport:
        if request.units:
            return self._export_units(request, is_canceled)
        report = self._export_legacy(request, is_canceled)
        unit_results = tuple(
            ExportUnitResult(
                unit_id=f"legacy-{index}",
                results=(result,),
                success=result.success,
                error=result.error,
                recovery_paths=(
                    (result.recovery_path,)
                    if result.recovery_path is not None
                    else ()
                ),
            )
            for index, result in enumerate(report.results)
        )
        return ExportReport(
            results=report.results,
            canceled=report.canceled,
            stopped_reason=report.stopped_reason,
            unit_results=unit_results,
        )

    def _export_legacy(
        self,
        request: ExportRequest,
        is_canceled: Callable[[], bool],
    ) -> ExportReport:
        (
            plans,
            reserved_targets,
            forbidden_sources,
            next_suffixes,
        ) = self._plan_batch(request)
        results: list[ExportItemResult] = []
        canceled = False
        stopped_reason: str | None = None
        destination_ready = False

        for plan in plans:
            item = plan.item
            target = plan.target
            quarantine: Path | None = None
            quarantine_committed = False
            target_promoted = False
            try:
                if is_canceled():
                    canceled = True
                    break
                if plan.error is not None:
                    raise plan.error
                if target is None:
                    raise ExportTargetError("无法解析导出目标")
                if not destination_ready:
                    request.destination.mkdir(parents=True, exist_ok=True)
                    destination_ready = True

                copy_source = item.source
                if request.mode is ExportMode.MOVE:
                    quarantine = self._quarantine_path(item.source)
                    copy_source = quarantine
                    self._quarantine_source(item.source, quarantine)
                    quarantine_committed = True
                    if is_canceled():
                        raise ExportCanceledError("导出已取消")

                while True:
                    try:
                        self._copy_verified(
                            copy_source,
                            target,
                            overwrite=(
                                request.conflict is ConflictPolicy.OVERWRITE
                            ),
                            is_canceled=is_canceled,
                        )
                    except ExportPromotionConflictError:
                        if request.conflict is not ConflictPolicy.UNIQUE:
                            raise
                        target = self._reserve_unique_target(
                            self._safe_target(
                                request.destination, item.target_name
                            ),
                            reserved_targets,
                            forbidden_sources,
                            next_suffixes,
                        )
                        continue
                    target_promoted = True
                    break

                if request.mode is ExportMode.MOVE:
                    try:
                        quarantine.unlink()
                    except Exception as exc:
                        raise ExportQuarantineError(
                            "目标已导出，但隔离源删除失败；"
                            f"可恢复文件保留在: {quarantine}: {exc}",
                            quarantine,
                        ) from exc
            except BaseException as exc:
                capacity_error = self._is_capacity_error(exc)
                capacity_source = exc
                if self._is_cancellation(exc):
                    canceled = True
                recovery_path = self._recovery_path(exc)
                restore_error: BaseException | None = None
                quarantine_exists = (
                    quarantine is not None and os.path.lexists(quarantine)
                )
                source_exists = os.path.lexists(item.source)
                rename_completed_despite_error = (
                    quarantine_exists
                    and not quarantine_committed
                    and not source_exists
                )

                if (
                    recovery_path is None
                    and target_promoted
                    and quarantine_exists
                ):
                    recovery_path = quarantine

                if (
                    quarantine_exists
                    and quarantine is not None
                    and not target_promoted
                ):
                    try:
                        self._restore_quarantine(quarantine, item.source)
                    except BaseException as caught_restore_error:
                        restore_error = caught_restore_error
                        recovery_path = quarantine
                        if self._is_capacity_error(caught_restore_error):
                            capacity_error = True
                            capacity_source = caught_restore_error
                    else:
                        if rename_completed_despite_error or not isinstance(
                            exc, Exception
                        ):
                            recovery_path = item.source

                if restore_error is not None and not isinstance(
                    restore_error, Exception
                ):
                    restore_error.add_note(
                        f"此前导出异常: {type(exc).__name__}: {exc}"
                    )
                    self._add_data_location_note(
                        restore_error, item.source, quarantine
                    )
                    raise restore_error
                if not isinstance(exc, Exception):
                    if restore_error is not None:
                        exc.add_note(
                            f"隔离源恢复失败: {type(restore_error).__name__}: "
                            f"{restore_error}"
                        )
                    if recovery_path is not None:
                        exc.add_note(f"可恢复文件保留在: {recovery_path}")
                    else:
                        self._add_data_location_note(
                            exc, item.source, quarantine
                        )
                    raise

                reported_error: Exception = exc
                if restore_error is not None:
                    reported_error = ExportQuarantineError(
                        f"{exc}; 隔离源恢复失败；"
                        f"可恢复文件保留在: {quarantine}: {restore_error}",
                        quarantine,  # type: ignore[arg-type]
                    )

                results.append(
                    ExportItemResult(
                        item.source,
                        target,
                        False,
                        str(reported_error),
                        recovery_path,
                    )
                )
                if canceled:
                    break
                if capacity_error:
                    stopped_reason = self._capacity_stop_reason(capacity_source)
                    break
            else:
                results.append(
                    ExportItemResult(item.source, target, True, None)
                )

        return ExportReport(
            tuple(results),
            canceled=canceled,
            stopped_reason=stopped_reason,
        )

    def _export_units(
        self,
        request: ExportRequest,
        is_canceled: Callable[[], bool],
    ) -> ExportReport:
        units = request.units
        request_error: ExportRequestError | None = None
        if request.items:
            units = units + tuple(
                ExportUnit(f"legacy-{index}", (item,))
                for index, item in enumerate(request.items)
            )
            request_error = ExportRequestError(
                "items 与 units 不能在同一导出请求中混用"
            )
        unit_ids = [unit.unit_id for unit in units]
        if any(not unit_id for unit_id in unit_ids):
            request_error = ExportRequestError("导出单元 ID 不能为空")
        elif len(set(unit_ids)) != len(unit_ids):
            request_error = ExportRequestError("导出单元 ID 不能重复")
        elif any(not unit.items for unit in units):
            request_error = ExportRequestError("导出单元不能为空")
        if request_error is not None:
            unit_results = tuple(
                ExportUnitResult(
                    unit.unit_id,
                    tuple(
                        ExportItemResult(
                            item.source,
                            None,
                            False,
                            str(request_error),
                        )
                        for item in unit.items
                    ),
                    False,
                    str(request_error),
                )
                for unit in units
            )
            return ExportReport(
                tuple(
                    result
                    for unit_result in unit_results
                    for result in unit_result.results
                ),
                False,
                unit_results=unit_results,
            )

        flat_items = tuple(
            item for unit in units for item in unit.items
        )
        planning_request = ExportRequest(
            destination=request.destination,
            items=flat_items,
            mode=request.mode,
            conflict=request.conflict,
        )
        plans, _, _, _ = self._plan_batch(planning_request)
        mutable_plans = list(plans)
        seen_move_sources: set[str] = set()
        for index, item in enumerate(flat_items):
            source_error: Exception | None = None
            try:
                source_key = self._filesystem_key(item.source)
                if (
                    request.mode is ExportMode.MOVE
                    and source_key in seen_move_sources
                ):
                    raise ExportRequestError(
                        f"MOVE 源文件重复: {item.source}"
                    )
                seen_move_sources.add(source_key)
                if not os.path.lexists(item.source):
                    raise FileNotFoundError(f"源文件不存在: {item.source}")
                if not item.source.is_file():
                    raise ExportRequestError(
                        f"源路径不是普通文件: {item.source}"
                    )
                if not os.access(item.source, os.R_OK):
                    raise PermissionError(f"源文件不可读: {item.source}")
            except Exception as error:
                source_error = error
            if source_error is not None and mutable_plans[index].error is None:
                plan = mutable_plans[index]
                mutable_plans[index] = _PlannedItem(
                    plan.item,
                    plan.target,
                    source_error,
                )
        plans = tuple(mutable_plans)
        planned_units: list[tuple[ExportUnit, tuple[_PlannedItem, ...]]] = []
        offset = 0
        for unit in units:
            unit_plans = plans[offset : offset + len(unit.items)]
            offset += len(unit.items)
            planned_units.append((unit, unit_plans))

        all_results: list[ExportItemResult] = []
        unit_results: list[ExportUnitResult] = []
        canceled = False
        stopped_reason: str | None = None

        for unit, unit_plans in planned_units:
            if is_canceled():
                canceled = True
                break
            plan_error = next(
                (plan.error for plan in unit_plans if plan.error is not None),
                None,
            )
            if plan_error is not None:
                results = tuple(
                    ExportItemResult(
                        plan.item.source,
                        plan.target,
                        False,
                        str(plan_error),
                    )
                    for plan in unit_plans
                )
                all_results.extend(results)
                unit_results.append(
                    ExportUnitResult(
                        unit.unit_id,
                        results,
                        False,
                        str(plan_error),
                    )
                )
                if self._is_capacity_error(plan_error):
                    stopped_reason = self._capacity_stop_reason(plan_error)
                    break
                continue

            try:
                results, cleanup_error, recovery_paths = self._execute_unit(
                    request,
                    unit_plans,
                    is_canceled,
                )
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                canceled = canceled or self._is_cancellation(error)
                recovery_paths = tuple(
                    path
                    for path in getattr(error, "recovery_paths", ())
                    if isinstance(path, Path)
                )
                results = tuple(
                    ExportItemResult(
                        plan.item.source,
                        plan.target,
                        False,
                        str(error),
                        next(
                            (
                                path
                                for path in recovery_paths
                                if path == plan.item.source
                                or path.name.startswith(
                                    f".{plan.item.source.name}."
                                )
                            ),
                            None,
                        ),
                    )
                    for plan in unit_plans
                )
                all_results.extend(results)
                unit_results.append(
                    ExportUnitResult(
                        unit.unit_id,
                        results,
                        False,
                        str(error),
                        recovery_paths,
                    )
                )
                if self._is_capacity_error(error):
                    stopped_reason = self._capacity_stop_reason(error)
                    break
                if canceled:
                    break
            else:
                all_results.extend(results)
                unit_results.append(
                    ExportUnitResult(
                        unit.unit_id,
                        results,
                        True,
                        cleanup_error,
                        recovery_paths,
                    )
                )

        return ExportReport(
            tuple(all_results),
            canceled,
            stopped_reason,
            tuple(unit_results),
        )

    def _execute_unit(
        self,
        request: ExportRequest,
        plans: tuple[_PlannedItem, ...],
        is_canceled: Callable[[], bool],
    ) -> tuple[
        tuple[ExportItemResult, ...],
        str | None,
        tuple[Path, ...],
    ]:
        states = [_UnitMemberState(plan) for plan in plans]
        request.destination.mkdir(parents=True, exist_ok=True)
        primary_error: BaseException | None = None
        recovery_paths: list[Path] = []
        try:
            if request.conflict is ConflictPolicy.OVERWRITE:
                for state in states:
                    target = state.plan.target
                    if target is not None and os.path.lexists(target):
                        backup = target.with_name(
                            f".{target.name}.{uuid4().hex}.backup"
                        )
                        state.backup = backup
                        os.replace(target, backup)

            if request.mode is ExportMode.MOVE:
                for state in states:
                    source = state.plan.item.source
                    quarantine = self._quarantine_path(source)
                    state.quarantine = quarantine
                    self._quarantine_source(source, quarantine)
                if is_canceled():
                    raise ExportCanceledError("导出已取消")

            for state in states:
                target = state.plan.target
                if target is None:
                    raise ExportTargetError("无法解析导出目标")
                partial = target.with_name(
                    f".{target.name}.{uuid4().hex}.partial"
                )
                state.partial = partial
                copy_source = (
                    state.quarantine
                    if state.quarantine is not None
                    else state.plan.item.source
                )
                shutil.copy2(copy_source, partial)
                if partial.stat().st_size != copy_source.stat().st_size:
                    raise ExportVerificationError("复制后文件大小不一致")
                if is_canceled():
                    raise ExportCanceledError("导出已取消")

            for state in states:
                target = state.plan.target
                partial = state.partial
                if target is None or partial is None:
                    raise ExportTargetError("导出事务状态不完整")
                if request.conflict is ConflictPolicy.OVERWRITE:
                    try:
                        os.replace(partial, target)
                    except BaseException:
                        if (
                            os.path.lexists(target)
                            and not os.path.lexists(partial)
                        ):
                            state.promoted = True
                            state.partial = None
                        raise
                    else:
                        state.promoted = True
                        state.partial = None
                else:
                    try:
                        os.link(partial, target)
                    except BaseException as error:
                        try:
                            state.promoted = (
                                os.path.lexists(target)
                                and os.path.lexists(partial)
                                and os.path.samefile(partial, target)
                            )
                        except OSError:
                            state.promoted = False
                        if (
                            isinstance(error, FileExistsError)
                            and not state.promoted
                        ):
                            raise ExportPromotionConflictError(
                                str(error)
                            ) from error
                        raise
                    else:
                        state.promoted = True
                    partial.unlink()
                    state.partial = None

            if request.mode is ExportMode.MOVE:
                for state in states:
                    if state.quarantine is not None:
                        state.quarantine.unlink()

        except BaseException as error:
            primary_error = error
            rollback_paths, rollback_errors = self._rollback_unit(states)
            recovery_paths.extend(rollback_paths)
            if rollback_errors:
                setattr(error, "rollback_errors", rollback_errors)
            recovery_paths = [
                path
                for path in dict.fromkeys(recovery_paths)
                if os.path.lexists(path)
            ]
            if recovery_paths:
                setattr(error, "recovery_paths", tuple(recovery_paths))
            if not isinstance(error, Exception):
                if recovery_paths:
                    error.add_note(
                        "可恢复文件保留在: "
                        + ", ".join(str(path) for path in recovery_paths)
                    )
                raise
            raise
        finally:
            cleanup_errors: list[str] = []
            for state in states:
                partial = state.partial
                if partial is None or not os.path.lexists(partial):
                    continue
                try:
                    partial.unlink()
                except BaseException as error:
                    if os.path.lexists(partial):
                        recovery_paths.append(partial)
                    if not isinstance(error, Exception):
                        if primary_error is not None:
                            error.add_note(
                                "此前导出异常: "
                                f"{type(primary_error).__name__}: "
                                f"{primary_error}"
                            )
                        existing_recovery = tuple(
                            path
                            for path in dict.fromkeys(recovery_paths)
                            if os.path.lexists(path)
                        )
                        if existing_recovery:
                            setattr(
                                error,
                                "recovery_paths",
                                existing_recovery,
                            )
                            error.add_note(
                                "可恢复文件保留在: "
                                + ", ".join(
                                    str(path) for path in existing_recovery
                                )
                            )
                        raise
                    if os.path.lexists(partial):
                        cleanup_errors.append(f"{partial}: {error}")
            existing_recovery = tuple(
                path
                for path in dict.fromkeys(recovery_paths)
                if os.path.lexists(path)
            )
            if primary_error is not None and existing_recovery:
                setattr(
                    primary_error,
                    "recovery_paths",
                    existing_recovery,
                )
            if cleanup_errors and primary_error is None:
                error = ExportCleanupError(
                    "临时文件清理失败: " + "; ".join(cleanup_errors),
                    existing_recovery[0],
                )
                setattr(error, "recovery_paths", existing_recovery)
                raise error

        cleanup_errors: list[str] = []
        cleanup_recovery: list[Path] = []
        for state in states:
            backup = state.backup
            if backup is None:
                continue
            try:
                backup.unlink(missing_ok=True)
            except BaseException as error:
                if not isinstance(error, Exception):
                    if os.path.lexists(backup):
                        error.add_note(f"覆盖备份保留在: {backup}")
                    raise
                if os.path.lexists(backup):
                    cleanup_errors.append(f"{backup}: {error}")
                    cleanup_recovery.append(backup)
            finally:
                if not os.path.lexists(backup):
                    state.backup = None

        results = tuple(
            ExportItemResult(
                state.plan.item.source,
                state.plan.target,
                True,
                None,
            )
            for state in states
        )
        cleanup_error = (
            "导出已完成，但覆盖备份清理失败: "
            + "; ".join(cleanup_errors)
            if cleanup_errors
            else None
        )
        return results, cleanup_error, tuple(cleanup_recovery)

    def _rollback_unit(
        self,
        states: list[_UnitMemberState],
    ) -> tuple[tuple[Path, ...], tuple[BaseException, ...]]:
        recovery_paths: list[Path] = []
        rollback_errors: list[BaseException] = []

        for state in reversed(states):
            source = state.plan.item.source
            quarantine = state.quarantine
            target = state.plan.target
            if os.path.lexists(source):
                continue
            try:
                if quarantine is not None and os.path.lexists(quarantine):
                    self._restore_quarantine(quarantine, source)
                elif target is not None and os.path.lexists(target):
                    shutil.copy2(target, source)
            except BaseException as error:
                rollback_errors.append(error)
                for path in (quarantine, target):
                    if path is not None and os.path.lexists(path):
                        recovery_paths.append(path)

        for state in reversed(states):
            target = state.plan.target
            target_is_recovery = (
                target is not None and target in recovery_paths
            )
            if (
                state.promoted
                and target is not None
                and not target_is_recovery
                and os.path.lexists(target)
            ):
                try:
                    target.unlink()
                except BaseException as error:
                    rollback_errors.append(error)
                    if os.path.lexists(target):
                        recovery_paths.append(target)
            backup = state.backup
            if backup is not None and os.path.lexists(backup):
                if target_is_recovery:
                    recovery_paths.append(backup)
                    continue
                try:
                    os.replace(backup, target)
                except BaseException as error:
                    rollback_errors.append(error)
                    if os.path.lexists(backup):
                        recovery_paths.append(backup)

        for state in reversed(states):
            for path in (state.partial, state.quarantine):
                if path is None or not os.path.lexists(path):
                    continue
                if path in recovery_paths:
                    continue
                try:
                    path.unlink()
                except BaseException as error:
                    rollback_errors.append(error)
                    if os.path.lexists(path):
                        recovery_paths.append(path)

        return (
            tuple(
                path
                for path in dict.fromkeys(recovery_paths)
                if os.path.lexists(path)
            ),
            tuple(rollback_errors),
        )

    def _plan_batch(
        self, request: ExportRequest
    ) -> tuple[
        tuple[_PlannedItem, ...],
        set[str],
        set[str],
        dict[str, int],
    ]:
        request_error: ExportRequestError | None = None
        if not isinstance(request.conflict, ConflictPolicy):
            request_error = ExportRequestError(
                f"无效的冲突策略: {request.conflict!r}"
            )
        elif not isinstance(request.mode, ExportMode):
            request_error = ExportRequestError(
                f"无效的导出模式: {request.mode!r}"
            )
        if request_error is not None:
            return (
                tuple(
                    _PlannedItem(item, None, request_error)
                    for item in request.items
                ),
                set(),
                set(),
                {},
            )

        forbidden_sources: set[str] = set()
        source_errors: dict[int, ExportTargetError] = {}
        for index, item in enumerate(request.items):
            try:
                forbidden_sources.add(self._filesystem_key(item.source))
            except ExportTargetError as exc:
                source_errors[index] = exc

        reserved_targets: set[str] = set()
        next_suffixes: dict[str, int] = {}
        plans: list[_PlannedItem] = []
        for index, item in enumerate(request.items):
            target: Path | None = None
            try:
                if index in source_errors:
                    raise source_errors[index]
                base_target = self._safe_target(
                    request.destination, item.target_name
                )
                target = self._reserve_target(
                    base_target,
                    request.conflict,
                    reserved_targets,
                    forbidden_sources,
                    next_suffixes,
                )
            except Exception as exc:
                if target is None:
                    try:
                        target = self._safe_target(
                            request.destination, item.target_name
                        )
                    except ExportTargetError:
                        target = None
                plans.append(_PlannedItem(item, target, exc))
            else:
                plans.append(_PlannedItem(item, target))

        return (
            tuple(plans),
            reserved_targets,
            forbidden_sources,
            next_suffixes,
        )

    def _reserve_target(
        self,
        target: Path,
        conflict: ConflictPolicy,
        reserved_targets: set[str],
        forbidden_sources: set[str],
        next_suffixes: dict[str, int],
    ) -> Path:
        target_key = self._filesystem_key(target)
        unavailable = (
            target_key in reserved_targets
            or target_key in forbidden_sources
            or os.path.lexists(target)
        )
        if conflict is ConflictPolicy.UNIQUE and unavailable:
            return self._reserve_unique_target(
                target,
                reserved_targets,
                forbidden_sources,
                next_suffixes,
            )
        if target_key in forbidden_sources:
            raise ExportTargetError(
                f"目标文件不能别名到本批次源文件: {target}"
            )
        if target_key in reserved_targets:
            raise ExportTargetError(f"批次内目标文件名重复: {target}")
        if os.path.lexists(target) and conflict is ConflictPolicy.SKIP:
            raise FileExistsError(f"目标文件已存在: {target}")

        reserved_targets.add(target_key)
        if conflict is ConflictPolicy.UNIQUE:
            next_suffixes.setdefault(target_key, 1)
        return target

    def _reserve_unique_target(
        self,
        target: Path,
        reserved_targets: set[str],
        forbidden_sources: set[str],
        next_suffixes: dict[str, int],
    ) -> Path:
        base_key = self._filesystem_key(target)
        index = next_suffixes.get(base_key, 1)
        while True:
            candidate = target.with_name(
                f"{target.stem} ({index}){target.suffix}"
            )
            index += 1
            candidate_key = self._filesystem_key(candidate)
            if (
                candidate_key in reserved_targets
                or candidate_key in forbidden_sources
                or os.path.lexists(candidate)
            ):
                continue
            next_suffixes[base_key] = index
            reserved_targets.add(candidate_key)
            return candidate

    def _canonical_path(self, path: Path) -> Path:
        try:
            return path.resolve()
        except RuntimeError as exc:
            raise ExportTargetError(f"无法解析源或目标路径: {exc}") from exc

    def _filesystem_key(self, path: Path) -> str:
        """Return a conservative key for common photo-volume equivalence."""
        resolved = os.path.normpath(os.fspath(self._canonical_path(path)))
        decomposed = unicodedata.normalize("NFD", resolved)
        return unicodedata.normalize("NFD", decomposed.casefold())

    def _is_cancellation(self, error: BaseException) -> bool:
        return any(
            isinstance(current, ExportCanceledError)
            for current in self._exception_chain(error)
        )

    def _is_capacity_error(self, error: BaseException) -> bool:
        capacity_codes = {errno.ENOSPC}
        if hasattr(errno, "EDQUOT"):
            capacity_codes.add(errno.EDQUOT)
        return any(
            isinstance(current, OSError)
            and current.errno in capacity_codes
            for current in self._exception_chain(error)
        )

    def _capacity_stop_reason(self, error: BaseException) -> str:
        quota_code = getattr(errno, "EDQUOT", None)
        if any(
            isinstance(current, OSError) and current.errno == quota_code
            for current in self._exception_chain(error)
        ):
            return "存储配额已用尽，已停止后续导出"
        return "磁盘空间不足，已停止后续导出"

    def _recovery_path(self, error: BaseException) -> Path | None:
        for current in self._exception_chain(error):
            recovery_path = getattr(current, "recovery_path", None)
            if isinstance(recovery_path, Path):
                return recovery_path
        return None

    def _exception_chain(self, error: BaseException) -> tuple[BaseException, ...]:
        chain: list[BaseException] = []
        seen: set[int] = set()
        pending: list[BaseException] = [error]
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            chain.append(current)
            linked = current.__cause__ or current.__context__
            if linked is not None:
                pending.append(linked)
            pending.extend(
                candidate
                for candidate in getattr(current, "rollback_errors", ())
                if isinstance(candidate, BaseException)
            )
        return tuple(chain)

    def _quarantine_path(self, source: Path) -> Path:
        return source.with_name(
            f".{source.name}.{uuid4().hex}.moving"
        )

    def _quarantine_source(self, source: Path, quarantine: Path) -> None:
        os.rename(source, quarantine)

    def _restore_quarantine(self, quarantine: Path, source: Path) -> None:
        os.link(quarantine, source, follow_symlinks=False)
        quarantine.unlink()

    def _add_data_location_note(
        self,
        error: BaseException,
        source: Path,
        quarantine: Path | None,
    ) -> None:
        locations = [
            path
            for path in (source, quarantine)
            if path is not None and os.path.lexists(path)
        ]
        if locations:
            error.add_note(
                "照片数据位置: " + ", ".join(str(path) for path in locations)
            )
        else:
            expected = [source]
            if quarantine is not None:
                expected.append(quarantine)
            error.add_note(
                "照片数据位置无法确认；请检查: "
                + ", ".join(str(path) for path in expected)
            )

    def _safe_target(self, destination: Path, target_name: str) -> Path:
        if (
            not target_name
            or target_name in {".", ".."}
            or "/" in target_name
            or "\\" in target_name
            or "\0" in target_name
        ):
            raise ExportTargetError(f"无效的目标文件名: {target_name!r}")
        return destination / target_name

    def _copy_verified(
        self,
        source: Path,
        target: Path,
        *,
        overwrite: bool,
        is_canceled: Callable[[], bool],
    ) -> None:
        temp = target.with_name(f".{target.name}.{uuid4().hex}.partial")
        primary_error: BaseException | None = None
        try:
            shutil.copy2(source, temp)
            if temp.stat().st_size != source.stat().st_size:
                raise ExportVerificationError("复制后文件大小不一致")
            if is_canceled():
                raise ExportCanceledError("导出已取消")
            if overwrite:
                os.replace(temp, target)
            else:
                try:
                    os.link(temp, target)
                except FileExistsError as exc:
                    raise ExportPromotionConflictError(str(exc)) from exc
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                temp.unlink(missing_ok=True)
            except BaseException as cleanup_error:
                if not isinstance(cleanup_error, Exception):
                    cleanup_error.add_note(
                        f"临时文件可能保留在: {temp}"
                    )
                    if primary_error is not None:
                        cleanup_error.add_note(
                            "此前导出异常: "
                            f"{type(primary_error).__name__}: {primary_error}"
                        )
                    raise
                if primary_error is None:
                    raise ExportCleanupError(
                        f"临时文件清理失败: {temp}: {cleanup_error}",
                        temp,
                    ) from cleanup_error
                if isinstance(primary_error, Exception) and isinstance(
                    cleanup_error, Exception
                ):
                    raise ExportCleanupError(
                        f"{primary_error}; 临时文件清理失败: "
                        f"{temp}: {cleanup_error}",
                        temp,
                    ) from primary_error
                primary_error.add_note(
                    f"临时文件清理失败: {temp}: {cleanup_error}"
                )
