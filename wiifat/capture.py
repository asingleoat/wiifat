"""Reusable stable-window capture for scale and calibration workflows."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import fmean, pstdev


@dataclass(frozen=True)
class CaptureSample:
    """One timestamped total/corner sample, with optional parallel raw total."""

    t: float
    total_kg: float
    corners: dict[str, float]
    raw_total_kg: float | None = None


@dataclass(frozen=True)
class StableCapture:
    """Means and dispersion from the first stable window found."""

    timestamp: float
    total_kg: float
    stdev_kg: float
    corners: dict[str, float]
    duration_s: float
    samples: tuple[CaptureSample, ...]
    raw_total_kg: float | None = None


class StableWindowCapture:
    """Find the first stable window above an optional presence threshold."""

    def __init__(
        self,
        *,
        minimum_total_kg: float | None = None,
        window_s: float = 2.5,
        max_stdev_kg: float = 0.2,
        max_sample_gap_s: float = 0.5,
    ) -> None:
        self.minimum_total_kg = minimum_total_kg
        self.window_s = window_s
        self.max_stdev_kg = max_stdev_kg
        self.max_sample_gap_s = max_sample_gap_s
        self._samples: deque[CaptureSample] = deque()
        self._last_t: float | None = None

    def reset(self) -> None:
        """Discard the current candidate window."""
        self._samples.clear()
        self._last_t = None

    def progress(self) -> tuple[float, float | None]:
        """Return candidate-window fill and dispersion without changing it."""

        if not self._samples:
            return 0.0, None
        span = self._samples[-1].t - self._samples[0].t
        fill = max(0.0, min(1.0, span / self.window_s))
        stdev_kg = (
            pstdev(item.total_kg for item in self._samples)
            if len(self._samples) >= 2
            else None
        )
        return fill, stdev_kg

    def update(
        self,
        t: float,
        total_kg: float,
        corners: dict[str, float],
        raw_total_kg: float | None = None,
    ) -> StableCapture | None:
        """Consume a sample and return the first complete stable window."""
        if self._last_t is not None and t < self._last_t:
            raise ValueError("sample timestamps must be nondecreasing")
        if self._last_t is not None and t - self._last_t > self.max_sample_gap_s:
            self._samples.clear()
        self._last_t = t

        if self.minimum_total_kg is not None and total_kg < self.minimum_total_kg:
            self._samples.clear()
            return None

        sample = CaptureSample(t, total_kg, dict(corners), raw_total_kg)
        self._samples.append(sample)
        cutoff = t - self.window_s
        # Retain the final observation on or before the boundary so the
        # buffered observations cover the complete interval.
        while len(self._samples) >= 2 and self._samples[1].t <= cutoff:
            self._samples.popleft()

        if t - self._samples[0].t < self.window_s - 1e-9:
            return None

        totals = [item.total_kg for item in self._samples]
        stdev_kg = pstdev(totals)
        if stdev_kg >= self.max_stdev_kg:
            return None

        samples = tuple(self._samples)
        keys = set().union(*(item.corners for item in samples))
        corner_means = {
            key: fmean(item.corners[key] for item in samples if key in item.corners)
            for key in sorted(keys)
        }
        raw_totals = [item.raw_total_kg for item in samples]
        raw_mean = (
            fmean(value for value in raw_totals if value is not None)
            if all(value is not None for value in raw_totals)
            else None
        )
        return StableCapture(
            timestamp=t,
            total_kg=fmean(totals),
            stdev_kg=stdev_kg,
            corners=corner_means,
            duration_s=t - samples[0].t,
            samples=samples,
            raw_total_kg=raw_mean,
        )
