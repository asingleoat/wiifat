"""Pure state machine for turning load-cell samples into weigh-ins."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median

from .capture import StableCapture, StableWindowCapture


@dataclass(frozen=True)
class Measurement:
    """A completed weight measurement.

    ``timestamp`` is the Unix timestamp of the sample that completed the
    stable window. Corner values are raw (not tared) kilograms.
    """

    timestamp: float
    weight_kg: float
    stdev_kg: float
    tare_kg: float
    corners: dict[str, float]
    duration_s: float
    raw_kg: float | None = None
    cal_json: str | None = None
    battery_pct: int | None = None
    id: int | None = None
    user_id: int | None = None
    assign_method: str | None = None
    assign_confidence: float | None = None

    @property
    def ts(self) -> float:
        """Short alias for callers that use the database column name."""
        return self.timestamp


class ScaleStateMachine:
    """Consume timestamped board samples and emit one result per occupancy."""

    IDLE = "IDLE"
    MEASURING = "MEASURING"
    MEASURED = "MEASURED"

    def __init__(
        self,
        *,
        baseline_window_s: float = 5.0,
        step_on_kg: float = 20.0,
        step_on_s: float = 0.5,
        stability_window_s: float = 2.5,
        stability_stdev_kg: float = 0.2,
        step_off_kg: float = 10.0,
        step_off_s: float = 1.0,
        max_sample_gap_s: float = 0.5,
    ) -> None:
        self.baseline_window_s = baseline_window_s
        self.step_on_kg = step_on_kg
        self.step_on_s = step_on_s
        self.stability_window_s = stability_window_s
        self.stability_stdev_kg = stability_stdev_kg
        self.step_off_kg = step_off_kg
        self.step_off_s = step_off_s
        self.max_sample_gap_s = max_sample_gap_s

        self.state = self.IDLE
        self._idle_samples: deque[tuple[float, float, float | None]] = deque()
        self._capture: StableWindowCapture | None = None
        self._tare_kg: float | None = None
        self._raw_tare_kg: float | None = None
        self._occupancy_tare_kg: float | None = None
        self._occupancy_raw_tare_kg: float | None = None
        self._step_on_since: float | None = None
        self._step_off_since: float | None = None
        self._last_t: float | None = None

    @property
    def tare_kg(self) -> float | None:
        """Current idle baseline, or ``None`` before the first sample."""
        return self._tare_kg

    def snapshot(self) -> dict[str, str | float | None]:
        """Return cheap read-only state and stable-window progress."""

        if self.state == self.MEASURING and self._capture is not None:
            fill, stdev_kg = self._capture.progress()
        elif self.state == self.MEASURED:
            fill, stdev_kg = 1.0, None
        else:
            fill, stdev_kg = 0.0, None
        return {"state": self.state, "fill": fill, "stdev_kg": stdev_kg}

    def update(
        self,
        t: float,
        total_kg: float,
        corners: dict[str, float],
        raw_total_kg: float | None = None,
    ) -> Measurement | None:
        """Process one sample, returning a measurement only when one completes."""
        if self._last_t is not None and t < self._last_t:
            raise ValueError("sample timestamps must be nondecreasing")

        gap = self._last_t is not None and t - self._last_t > self.max_sample_gap_s
        self._last_t = t
        if gap:
            self._reset_debounces_after_gap()

        if self.state == self.IDLE:
            return self._update_idle(t, total_kg, corners, raw_total_kg)
        if self.state == self.MEASURING:
            return self._update_measuring(t, total_kg, corners, raw_total_kg)
        return self._update_measured(t, total_kg, corners, raw_total_kg)

    def _update_idle(
        self,
        t: float,
        total_kg: float,
        corners: dict[str, float],
        raw_total_kg: float | None,
    ) -> Measurement | None:
        if self._tare_kg is None:
            self._record_idle(t, total_kg, raw_total_kg)
            return None

        if total_kg >= self._tare_kg + self.step_on_kg:
            if self._step_on_since is None:
                self._step_on_since = t
                self._occupancy_tare_kg = self._tare_kg
                self._occupancy_raw_tare_kg = self._raw_tare_kg
                self._capture = StableWindowCapture(
                    minimum_total_kg=self._tare_kg + self.step_on_kg,
                    window_s=self.stability_window_s,
                    max_stdev_kg=self.stability_stdev_kg,
                    max_sample_gap_s=self.max_sample_gap_s,
                )
            capture = self._stable_capture(t, total_kg, corners, raw_total_kg)

            if t - self._step_on_since >= self.step_on_s:
                self.state = self.MEASURING
                return self._measurement_from(capture) if capture is not None else None
            return None

        self._step_on_since = None
        self._occupancy_tare_kg = None
        self._occupancy_raw_tare_kg = None
        self._capture = None
        if total_kg < self._tare_kg + self.step_off_kg:
            self._record_idle(t, total_kg, raw_total_kg)
        return None

    def _update_measuring(
        self,
        t: float,
        total_kg: float,
        corners: dict[str, float],
        raw_total_kg: float | None,
    ) -> Measurement | None:
        tare = self._occupied_tare()
        if total_kg < tare + self.step_off_kg:
            if self._capture is not None:
                self._capture.reset()
            if self._step_off_since is None:
                self._step_off_since = t
            elif t - self._step_off_since >= self.step_off_s:
                self._return_to_idle(t, total_kg, raw_total_kg)
            return None

        self._step_off_since = None
        capture = self._stable_capture(t, total_kg, corners, raw_total_kg)
        return self._measurement_from(capture) if capture is not None else None

    def _update_measured(
        self,
        t: float,
        total_kg: float,
        _corners: dict[str, float],
        raw_total_kg: float | None,
    ) -> None:
        tare = self._occupied_tare()
        if total_kg < tare + self.step_off_kg:
            if self._step_off_since is None:
                self._step_off_since = t
            elif t - self._step_off_since >= self.step_off_s:
                self._return_to_idle(t, total_kg, raw_total_kg)
        else:
            self._step_off_since = None
        return None

    def _record_idle(
        self, t: float, total_kg: float, raw_total_kg: float | None
    ) -> None:
        self._idle_samples.append((t, total_kg, raw_total_kg))
        cutoff = t - self.baseline_window_s
        while self._idle_samples and self._idle_samples[0][0] < cutoff:
            self._idle_samples.popleft()
        self._tare_kg = median(value for _, value, _ in self._idle_samples)
        raw_values = [value for _, _, value in self._idle_samples]
        self._raw_tare_kg = (
            median(value for value in raw_values if value is not None)
            if all(value is not None for value in raw_values)
            else None
        )

    def _stable_capture(
        self,
        t: float,
        total_kg: float,
        corners: dict[str, float],
        raw_total_kg: float | None,
    ) -> StableCapture | None:
        if self._capture is None:
            raise RuntimeError("occupied state has no stable-window capture")
        return self._capture.update(t, total_kg, corners, raw_total_kg)

    def _measurement_from(self, capture: StableCapture) -> Measurement:
        tare = self._occupied_tare()
        measurement = Measurement(
            timestamp=capture.timestamp,
            weight_kg=capture.total_kg - tare,
            stdev_kg=capture.stdev_kg,
            tare_kg=tare,
            corners=capture.corners,
            duration_s=capture.timestamp - self._step_on_time(),
            raw_kg=self._raw_weight(capture),
        )
        self.state = self.MEASURED
        self._capture = None
        self._step_off_since = None
        return measurement

    def _raw_weight(self, capture: StableCapture) -> float | None:
        if capture.raw_total_kg is None or self._occupancy_raw_tare_kg is None:
            return None
        return capture.raw_total_kg - self._occupancy_raw_tare_kg

    def _return_to_idle(
        self, t: float, total_kg: float, raw_total_kg: float | None
    ) -> None:
        self.state = self.IDLE
        self._step_on_since = None
        self._step_off_since = None
        self._occupancy_tare_kg = None
        self._occupancy_raw_tare_kg = None
        self._capture = None
        self._record_idle(t, total_kg, raw_total_kg)

    def _reset_debounces_after_gap(self) -> None:
        self._step_off_since = None
        if self._capture is not None:
            self._capture.reset()
        if self.state == self.IDLE:
            self._step_on_since = None
            self._occupancy_tare_kg = None
            self._occupancy_raw_tare_kg = None
            self._capture = None

    def _occupied_tare(self) -> float:
        if self._occupancy_tare_kg is None:
            raise RuntimeError("occupied state has no tare")
        return self._occupancy_tare_kg

    def _step_on_time(self) -> float:
        if self._step_on_since is None:
            raise RuntimeError("occupied state has no step-on time")
        return self._step_on_since


# A concise alternative for callers that do not need to distinguish it from
# other state machines in their application.
StateMachine = ScaleStateMachine
