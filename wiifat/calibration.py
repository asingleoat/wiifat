"""Matrix-based Wii Balance Board calibration and affine correction."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Iterable, Mapping, Sequence


CORNERS = ("top-right", "bottom-right", "top-left", "bottom-left")
DEFAULT_CONFIG_PATH = Path("~/.local/share/wiifat/calibration.json").expanduser()


class CalibrationConvergenceError(RuntimeError):
    """Raised when alternating least squares does not converge."""


@dataclass(frozen=True)
class Calibration:
    """Versioned affine calibration plus fit diagnostics and source data."""

    version: int
    ts: str | None
    ref_weights_kg: tuple[float, ...]
    gains: dict[str, float]
    offsets: dict[str, float]
    residual_rms: dict[str, float]
    excluded_placements: int
    placements: tuple[tuple[float, ...], ...]
    placement_weights_kg: tuple[float, ...] = ()
    placement_bases: tuple[tuple[str, ...], ...] = ()
    placement_known_kg: tuple[float, ...] = ()
    fitted_bases_kg: dict[str, float] = field(default_factory=dict)
    known_total_rms_kg: float | None = None
    differential_rms_kg: float | None = None
    exactly_determined_offsets: tuple[str, ...] = ()
    iterations: int = 0
    final_delta: float = 0.0

    @classmethod
    def identity(cls) -> Calibration:
        """Return the transform used when no calibration file exists."""
        return cls(
            version=2,
            ts=None,
            ref_weights_kg=(),
            gains={corner: 1.0 for corner in CORNERS},
            offsets={corner: 0.0 for corner in CORNERS},
            residual_rms={corner: 0.0 for corner in CORNERS},
            excluded_placements=0,
            placements=(),
            placement_weights_kg=(),
            placement_bases=(),
            placement_known_kg=(),
            fitted_bases_kg={},
        )

    @property
    def warnings(self) -> list[str]:
        """Return exclusion and sanity warnings for this fit."""
        messages: list[str] = []
        if self.excluded_placements:
            messages.append(
                f"Excluded {self.excluded_placements} placement(s) because at least "
                "one cell was clamped at 0."
            )
        if self.placements and self.excluded_placements / len(self.placements) > 0.25:
            messages.append(
                "More than 25% of placements were excluded; use a heavier reference "
                "or keep placements away from the extreme corners."
            )
        for corner in CORNERS:
            gain = self.gains[corner]
            if not 0.8 <= gain <= 1.2:
                messages.append(f"{corner} gain {gain:.6f} is outside [0.8, 1.2].")
            residual = self.residual_rms[corner]
            if residual > 0.05:
                messages.append(
                    f"{corner} residual RMS {residual:.6f} kg exceeds 0.05 kg."
                )
        if self.offsets["bottom-left"] > -0.05:
            messages.append("bottom-left: no clamped offset detected")
        return messages

    def to_dict(self) -> dict[str, object]:
        """Return the version-2 on-disk representation."""
        return {
            "version": self.version,
            "ts": self.ts,
            "ref_weights_kg": list(self.ref_weights_kg),
            "gains": {corner: self.gains[corner] for corner in CORNERS},
            "offsets": {corner: self.offsets[corner] for corner in CORNERS},
            "residual_rms": {corner: self.residual_rms[corner] for corner in CORNERS},
            "excluded_placements": self.excluded_placements,
            "placements": [list(row) for row in self.placements],
            "placement_weights_kg": list(self.placement_weights_kg),
            "placement_bases": [list(bases) for bases in self.placement_bases],
            "placement_known_kg": list(self.placement_known_kg),
            "fitted_bases_kg": dict(self.fitted_bases_kg),
            "known_total_rms_kg": self.known_total_rms_kg,
            "differential_rms_kg": self.differential_rms_kg,
            "exactly_determined_offsets": list(self.exactly_determined_offsets),
        }

    def snapshot_json(self) -> str:
        """Return a compact immutable snapshot suitable for a database row."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def fit_calibration(
    empty_captures: Iterable[Mapping[str, float] | Sequence[float]],
    placements: Iterable[
        tuple[float, Mapping[str, float] | Sequence[float]]
        | tuple[str | None, float, Mapping[str, float] | Sequence[float]]
        | tuple[
            Sequence[str],
            float,
            Mapping[str, float] | Sequence[float],
        ]
    ],
    *,
    ridge: float = 1e-9,
    tolerance: float = 1e-7,
    max_iterations: int = 20_000,
    timestamp: str | None = None,
) -> Calibration:
    """Fit per-cell gains and offsets using constrained alternating least squares.

    Placement locations are deliberately unknown. Mechanical load sharing is
    represented by one latent four-cell load vector per placement, constrained
    to be nonnegative and sum to either a known total or a fitted shared base
    plus a known addition. A base combination can contain several unknown
    object masses; its coefficients are represented by repeated base ids.
    """
    if ridge < 0:
        raise ValueError("ridge must be nonnegative")

    empty = [_corner_vector(row) for row in empty_captures]
    tagged_placements = [_normalise_placement(item) for item in placements]
    if any(known_kg < 0.0 for _, known_kg, _ in tagged_placements):
        raise ValueError("known mass terms must be nonnegative")
    if any(
        not bases and known_kg <= 0.0
        for bases, known_kg, _ in tagged_placements
    ):
        raise ValueError("fully-known placement totals must be positive")
    if not empty:
        raise ValueError("at least one empty capture is required")

    usable = [
        (bases, known_kg, row)
        for bases, known_kg, row in tagged_placements
        if all(value != 0.0 for value in row)
    ]
    excluded = len(tagged_placements) - len(usable)
    if len(usable) < 4:
        raise ValueError("at least four unclamped placements are required")
    anchored_cells = [
        any(row[cell] > 0.0 for row in empty)
        for cell in range(len(CORNERS))
    ]
    unanchored_cells = [
        cell
        for cell in range(len(CORNERS))
        if not anchored_cells[cell]
    ]
    fully_known_totals = tuple(
        dict.fromkeys(
            known_kg for bases, known_kg, _ in usable if not bases
        )
    )
    combination_offsets: dict[tuple[str, ...], set[float]] = {}
    for bases, known_kg, _ in usable:
        if bases:
            combination_offsets.setdefault(bases, set()).add(known_kg)
    raw_base_ids = {
        base_id for bases, _, _ in tagged_placements for base_id in bases
    }
    usable_base_ids = {base_id for bases, _, _ in usable for base_id in bases}
    missing_bases = raw_base_ids - usable_base_ids
    if missing_bases:
        raise ValueError(
            "no usable placements remain for base(s): "
            + ", ".join(sorted(missing_bases))
        )

    has_absolute_anchor = bool(fully_known_totals) or any(
        known_kg > 0.0 for _, known_kg, _ in usable
    )
    if not has_absolute_anchor:
        raise ValueError(
            "calibration has no known mass anchor; add a fully-known placement or "
            "a nonzero known mass beside a shared reference object"
        )
    gauge_is_broken = len(fully_known_totals) >= 2 or any(
        len(offsets) >= 2 for offsets in combination_offsets.values()
    )
    if not gauge_is_broken:
        raise ValueError(
            "gain scale is unanchored; calibration needs placements at two "
            "different known masses or a shared object captured with two different "
            "known mass additions"
        )
    for cell in unanchored_cells:
        engaging_combinations = [
            bases for bases, _known_kg, row in usable if row[cell] != 0.0
        ]
        if not offset_is_identifiable(engaging_combinations):
            raise ValueError(
                f"cannot separate {CORNERS[cell]}'s hidden offset: add a closure "
                "step (capture two heavy objects separately and together) or a "
                "known-total capture engaging that corner."
            )

    exactly_determined_offsets = tuple(
        CORNERS[cell]
        for cell in unanchored_cells
        if _offset_has_no_internal_redundancy(
            [
                bases
                for bases, known_kg, row in usable
                if row[cell] != 0.0 and (not bases or known_kg == 0.0)
            ]
        )
    )

    gains = [1.0] * len(CORNERS)
    offsets = []
    for cell in range(len(CORNERS)):
        unclamped = [row[cell] for row in empty if row[cell] > 0.0]
        offsets.append(fmean(unclamped) if unclamped else 0.0)

    final_delta = math.inf
    bases = {base_id: 0.0 for base_id in usable_base_ids}
    for iteration in range(1, max_iterations + 1):
        next_bases = _solve_bases(usable, gains, offsets)
        loads = [
            _solve_loads(
                row,
                gains,
                offsets,
                _placement_total(base_ids, known_kg, next_bases),
            )
            for base_ids, known_kg, row in usable
        ]
        next_gains: list[float] = []
        next_offsets: list[float] = []
        for cell in range(len(CORNERS)):
            load_values = [load[cell] for load in loads]
            measured_values = [row[cell] for _, _, row in usable]
            for row in empty:
                if row[cell] > 0.0:
                    load_values.append(0.0)
                    measured_values.append(row[cell])
            # Ridge is only a numerical tie-break for degenerate placement
            # sets. Any appreciable pull toward one biases an otherwise
            # identifiable, well-spread calibration and displaces offsets.
            gain, offset = _ridge_line_fit(load_values, measured_values, ridge)
            next_gains.append(gain)
            next_offsets.append(offset)

        final_delta = max(
            *(abs(new - old) for new, old in zip(next_gains, gains)),
            *(abs(new - old) for new, old in zip(next_offsets, offsets)),
            *(
                abs(next_bases[base_id] - bases[base_id])
                for base_id in next_bases
            ),
        )
        gains = next_gains
        offsets = next_offsets
        bases = next_bases
        if final_delta < tolerance:
            break
    else:
        raise CalibrationConvergenceError(
            f"calibration did not converge after {max_iterations} iterations; "
            f"last parameter delta was {final_delta:.9g}"
        )

    bases = _solve_bases(usable, gains, offsets)
    totals = [
        _placement_total(base_ids, known_kg, bases)
        for base_ids, known_kg, _ in usable
    ]
    loads = [
        _solve_loads(row, gains, offsets, total)
        for (_, _, row), total in zip(usable, totals)
    ]
    residuals: list[float] = []
    for cell in range(len(CORNERS)):
        errors = [
            row[cell] - gains[cell] * load[cell] - offsets[cell]
            for (_, _, row), load in zip(usable, loads)
        ]
        errors.extend(
            row[cell] - offsets[cell] for row in empty if row[cell] > 0.0
        )
        residuals.append(math.sqrt(fmean(error * error for error in errors)))

    total_estimates = [
        _unconstrained_total(row, gains, offsets) for _, _, row in usable
    ]
    known_errors = [
        estimate - known_kg
        for estimate, (base_id, known_kg, _) in zip(total_estimates, usable)
        if not base_id
    ]
    differential_errors = [
        estimate - _placement_total(base_ids, known_kg, bases)
        for estimate, (base_ids, known_kg, _) in zip(total_estimates, usable)
        if base_ids
    ]
    return Calibration(
        version=2,
        ts=timestamp or _utc_now(),
        ref_weights_kg=fully_known_totals,
        gains=dict(zip(CORNERS, gains)),
        offsets=dict(zip(CORNERS, offsets)),
        residual_rms=dict(zip(CORNERS, residuals)),
        excluded_placements=excluded,
        placements=tuple(tuple(row) for _, _, row in tagged_placements),
        placement_weights_kg=tuple(
            _placement_total(base_ids, known_kg, bases)
            for base_ids, known_kg, _ in tagged_placements
        ),
        placement_bases=tuple(
            base_ids for base_ids, _, _ in tagged_placements
        ),
        placement_known_kg=tuple(
            known_kg for _, known_kg, _ in tagged_placements
        ),
        fitted_bases_kg=bases,
        known_total_rms_kg=_rms_or_none(known_errors),
        differential_rms_kg=_rms_or_none(differential_errors),
        exactly_determined_offsets=exactly_determined_offsets,
        iterations=iteration,
        final_delta=final_delta,
    )


def apply_calibration(
    readings: Mapping[str, float],
    calibration: Calibration | None,
) -> dict[str, float]:
    """Apply the affine ingestion correction to one four-cell frame.

    A zero report has an ambiguous true load because of kernel clamping, but
    idle is the common case and then zero is exact. When a person stands on the
    board every cell is engaged and the affine correction is exact. The
    ambiguous region is therefore limited to transient step-on/off, while the
    downstream state machine's dynamic tare absorbs residual drift.
    """
    selected = calibration or Calibration.identity()
    corrected: dict[str, float] = {}
    for corner in CORNERS:
        measured = float(readings[corner])
        gain = selected.gains[corner]
        if gain == 0.0:
            raise ValueError(f"{corner} calibration gain is zero")
        corrected[corner] = (
            0.0 if measured == 0.0 else (measured - selected.offsets[corner]) / gain
        )
    return corrected


def save_calibration(
    calibration: Calibration,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Pretty-print a version-2 calibration file and return its path."""
    output_path = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(calibration.to_dict(), indent=2) + "\n")
    return output_path


def load_calibration(
    path: str | os.PathLike[str] | None = None,
) -> Calibration | None:
    """Load and validate a calibration, returning ``None`` when absent."""
    input_path = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH
    if not input_path.exists():
        return None
    data = json.loads(input_path.read_text())
    if not isinstance(data, dict) or data.get("version") != 2:
        raise ValueError("calibration config must be a version-2 JSON object")

    gains = _named_values(data.get("gains"), "gains")
    offsets = _named_values(data.get("offsets"), "offsets")
    residuals = _named_values(data.get("residual_rms"), "residual_rms")
    if any(value == 0.0 for value in gains.values()):
        raise ValueError("calibration gains must be nonzero")
    placements_value = data.get("placements")
    if not isinstance(placements_value, list):
        raise ValueError("calibration placements must be a list")
    placements = tuple(tuple(_corner_vector(row)) for row in placements_value)
    ref_weights_value = data.get("ref_weights_kg")
    if not isinstance(ref_weights_value, list):
        raise ValueError("calibration ref_weights_kg must be a list")
    ref_weights = tuple(float(value) for value in ref_weights_value)
    placement_weights_value = data.get("placement_weights_kg")
    if not isinstance(placement_weights_value, list):
        raise ValueError("calibration placement_weights_kg must be a list")
    placement_weights = tuple(float(value) for value in placement_weights_value)
    if len(placement_weights) != len(placements):
        raise ValueError("calibration placement weights and readings differ in length")
    placement_bases_value = data.get("placement_bases")
    legacy_base_ids_value = data.get("placement_base_ids")
    if placement_bases_value is not None and legacy_base_ids_value is not None:
        raise ValueError(
            "calibration must not contain both placement_bases and "
            "placement_base_ids"
        )
    if placement_bases_value is None and legacy_base_ids_value is None:
        placement_bases: tuple[tuple[str, ...], ...] = ((),) * len(placements)
    elif isinstance(placement_bases_value, list):
        placement_bases = tuple(
            _base_combination(value) for value in placement_bases_value
        )
    elif isinstance(legacy_base_ids_value, list):
        placement_bases = tuple(
            () if value is None else (_base_id(value),)
            for value in legacy_base_ids_value
        )
    else:
        raise ValueError("calibration placement_bases must be a list of lists")
    placement_known_value = data.get("placement_known_kg")
    if placement_known_value is None:
        placement_known = placement_weights
    elif isinstance(placement_known_value, list):
        placement_known = tuple(float(value) for value in placement_known_value)
    else:
        raise ValueError("calibration placement_known_kg must be a list")
    if (
        len(placement_bases) != len(placements)
        or len(placement_known) != len(placements)
    ):
        raise ValueError("calibration placement tags and readings differ in length")
    fitted_bases_value = data.get("fitted_bases_kg", {})
    if not isinstance(fitted_bases_value, dict):
        raise ValueError("calibration fitted_bases_kg must be an object")
    fitted_bases = {
        str(base_id): float(weight)
        for base_id, weight in fitted_bases_value.items()
    }
    ts = data.get("ts")
    if ts is not None and not isinstance(ts, str):
        raise ValueError("calibration ts must be a string or null")
    return Calibration(
        version=2,
        ts=ts,
        ref_weights_kg=ref_weights,
        gains=gains,
        offsets=offsets,
        residual_rms=residuals,
        excluded_placements=int(data.get("excluded_placements", 0)),
        placements=placements,
        placement_weights_kg=placement_weights,
        placement_bases=placement_bases,
        placement_known_kg=placement_known,
        fitted_bases_kg=fitted_bases,
        known_total_rms_kg=_optional_float(data.get("known_total_rms_kg")),
        differential_rms_kg=_optional_float(data.get("differential_rms_kg")),
        exactly_determined_offsets=_corner_names(
            data.get("exactly_determined_offsets", [])
        ),
    )


def _normalise_placement(
    item: tuple[float, Mapping[str, float] | Sequence[float]]
    | tuple[
        str | None | Sequence[str],
        float,
        Mapping[str, float] | Sequence[float],
    ],
) -> tuple[tuple[str, ...], float, list[float]]:
    if len(item) == 2:
        known_kg, row = item
        return (), float(known_kg), _corner_vector(row)
    if len(item) == 3:
        bases_value, known_kg, row = item
        if bases_value is None:
            bases = ()
        elif isinstance(bases_value, str):
            bases = (_base_id(bases_value),)
        else:
            bases = _base_combination(bases_value)
        return bases, float(known_kg), _corner_vector(row)
    raise ValueError(
        "placement must be (known_kg, readings) or "
        "(bases, known_kg, readings)"
    )


def _base_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("placement base id must be a nonempty string")
    return value


def _base_combination(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("placement bases must be a list or tuple of base ids")
    return tuple(sorted(_base_id(base_id) for base_id in value))


def offset_is_identifiable(
    base_combinations: Sequence[Sequence[str]],
    *,
    tolerance: float = 1e-10,
) -> bool:
    """Return whether capture closure separates a cell's hidden offset.

    Each row contains the base objects present in a usable capture. A constant
    offset is identifiable exactly when an appended all-ones column adds a
    dimension to the base-combination design matrix. An empty combination is a
    fully-known capture and therefore anchors the offset directly.
    """
    base_ids = sorted(
        {base_id for combination in base_combinations for base_id in combination}
    )
    design = [
        [float(combination.count(base_id)) for base_id in base_ids]
        for combination in base_combinations
    ]
    augmented = [row + [1.0] for row in design]
    return _matrix_rank(augmented, tolerance) > _matrix_rank(design, tolerance)


def _offset_has_no_internal_redundancy(
    base_combinations: Sequence[Sequence[str]],
) -> bool:
    """Return whether every zero-addition closure capture is indispensable."""
    combinations = [tuple(combination) for combination in base_combinations]
    return bool(combinations) and offset_is_identifiable(combinations) and all(
        not offset_is_identifiable(
            combinations[:index] + combinations[index + 1 :]
        )
        for index in range(len(combinations))
    )


def _matrix_rank(matrix: Sequence[Sequence[float]], tolerance: float = 1e-10) -> int:
    """Compute the rank of a tiny dense matrix by pivoted elimination."""
    if not matrix:
        return 0
    width = len(matrix[0])
    if any(len(row) != width for row in matrix):
        raise ValueError("matrix rows must have equal length")
    work = [[float(value) for value in row] for row in matrix]
    rank = 0
    for column in range(width):
        pivot = max(
            range(rank, len(work)),
            key=lambda row: abs(work[row][column]),
            default=rank,
        )
        if pivot >= len(work) or abs(work[pivot][column]) <= tolerance:
            continue
        work[rank], work[pivot] = work[pivot], work[rank]
        pivot_value = work[rank][column]
        for row in range(rank + 1, len(work)):
            factor = work[row][column] / pivot_value
            if abs(factor) <= tolerance:
                continue
            for right in range(column, width):
                work[row][right] -= factor * work[rank][right]
        rank += 1
        if rank == len(work):
            break
    return rank


def _solve_linear_system(
    matrix: Sequence[Sequence[float]],
    rhs: Sequence[float],
    tolerance: float = 1e-12,
) -> list[float]:
    """Solve a tiny square dense system with pivoted Gaussian elimination."""
    size = len(matrix)
    if size == 0 or len(rhs) != size or any(len(row) != size for row in matrix):
        raise ValueError("base-object normal equations must be nonempty and square")
    work = [
        [float(value) for value in row] + [float(target)]
        for row, target in zip(matrix, rhs)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) <= tolerance:
            raise ValueError(
                "base combinations do not independently identify all object weights"
            )
        work[column], work[pivot] = work[pivot], work[column]
        pivot_value = work[column][column]
        for right in range(column, size + 1):
            work[column][right] /= pivot_value
        for row in range(size):
            if row == column:
                continue
            factor = work[row][column]
            for right in range(column, size + 1):
                work[row][right] -= factor * work[column][right]
    return [work[row][size] for row in range(size)]


def _solve_bases(
    placements: Sequence[tuple[tuple[str, ...], float, Sequence[float]]],
    gains: Sequence[float],
    offsets: Sequence[float],
) -> dict[str, float]:
    # Every total-constraint SSE has the same gain-dependent denominator, so
    # Step X is an unweighted least-squares problem in the base-object masses.
    base_ids = sorted(
        {base_id for bases, _, _ in placements for base_id in bases}
    )
    if not base_ids:
        return {}
    design: list[list[float]] = []
    targets: list[float] = []
    for bases, known_kg, measured in placements:
        if not bases:
            continue
        design.append([float(bases.count(base_id)) for base_id in base_ids])
        targets.append(
            _unconstrained_total(measured, gains, offsets) - known_kg
        )
    normal = [
        [
            sum(row[left] * row[right] for row in design)
            for right in range(len(base_ids))
        ]
        for left in range(len(base_ids))
    ]
    rhs = [
        sum(row[column] * target for row, target in zip(design, targets))
        for column in range(len(base_ids))
    ]
    solution = _solve_linear_system(normal, rhs)
    return dict(zip(base_ids, solution))


def _unconstrained_total(
    measured: Sequence[float],
    gains: Sequence[float],
    offsets: Sequence[float],
) -> float:
    return sum(
        (measured[cell] - offsets[cell]) / gains[cell]
        for cell in range(len(CORNERS))
    )


def _placement_total(
    base_ids: Sequence[str],
    known_kg: float,
    bases: Mapping[str, float],
) -> float:
    return known_kg + sum(bases[base_id] for base_id in base_ids)


def _rms_or_none(errors: Sequence[float]) -> float | None:
    if not errors:
        return None
    return math.sqrt(fmean(error * error for error in errors))


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _solve_loads(
    measured: Sequence[float],
    gains: Sequence[float],
    offsets: Sequence[float],
    ref_weight_kg: float,
) -> list[float]:
    """Solve the nonnegative, fixed-sum Step L subproblem."""
    if any(gain == 0.0 for gain in gains):
        raise CalibrationConvergenceError("zero gain encountered during Step L")
    loads = [0.0] * len(CORNERS)
    active = list(range(len(CORNERS)))
    while active:
        numerator = sum(
            (measured[cell] - offsets[cell]) / gains[cell] for cell in active
        ) - ref_weight_kg
        denominator = sum(1.0 / gains[cell] ** 2 for cell in active)
        c = numerator / denominator
        candidates = {
            cell: (measured[cell] - offsets[cell]) / gains[cell]
            - c / gains[cell] ** 2
            for cell in active
        }
        negative = [cell for cell, value in candidates.items() if value < 0.0]
        if not negative:
            for cell, value in candidates.items():
                loads[cell] = value
            return loads
        active = [cell for cell in active if cell not in negative]
    raise CalibrationConvergenceError("nonnegative Step L problem has no active cells")


def _ridge_line_fit(
    loads: Sequence[float],
    measured: Sequence[float],
    ridge: float,
) -> tuple[float, float]:
    mean_load = fmean(loads)
    mean_measured = fmean(measured)
    numerator = sum(
        (load - mean_load) * (value - mean_measured)
        for load, value in zip(loads, measured)
    ) + ridge
    denominator = sum((load - mean_load) ** 2 for load in loads) + ridge
    gain = numerator / denominator
    offset = mean_measured - gain * mean_load
    return gain, offset


def _corner_vector(row: Mapping[str, float] | Sequence[float]) -> list[float]:
    if isinstance(row, Mapping):
        try:
            return [float(row[corner]) for corner in CORNERS]
        except KeyError as exc:
            raise ValueError(f"missing corner {exc.args[0]}") from exc
    if isinstance(row, (str, bytes)) or len(row) != len(CORNERS):
        raise ValueError("corner row must have exactly four values")
    return [float(value) for value in row]


def _named_values(value: object, name: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(CORNERS):
        raise ValueError(f"calibration {name} must contain exactly the four corners")
    return {corner: float(value[corner]) for corner in CORNERS}


def _corner_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(corner, str) or corner not in CORNERS for corner in value
    ):
        raise ValueError(
            "calibration exactly_determined_offsets must be a list of corner names"
        )
    if len(set(value)) != len(value):
        raise ValueError("calibration exactly_determined_offsets contains duplicates")
    return tuple(value)


def _utc_now() -> str:
    return (
        datetime.fromtimestamp(time.time(), timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
