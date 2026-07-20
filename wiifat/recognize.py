"""Pure Bayesian recognition and sequential user-weight belief updates.

Assignments update a user's belief immediately. Reassigning or unassigning a
measurement does not undo an earlier belief update; that is an intentional POC
limitation and should be replaced by replayable model fitting in a mature app.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


SECONDS_PER_DAY = 86_400.0
UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecognitionParams:
    """Tunable recognition constants, expressed in kilograms and days."""

    drift_kg_per_sqrt_day: float = 0.2
    observation_sigma_kg: float = 0.6
    sigma_floor_kg: float = 0.5
    predictive_sigma_cap_kg: float = 8.0
    unknown_min_kg: float = 10.0
    unknown_max_kg: float = 250.0
    unknown_pseudo_count: float = 1.0
    auto_assign_threshold: float = 0.90
    new_user_sigma_kg: float = 2.0


@dataclass(frozen=True)
class UserModel:
    """Recognition fields for one named user, independent of storage."""

    id: int
    name: str
    mu_kg: float | None
    sigma_kg: float | None
    last_seen_ts: float | None
    weigh_count: int


@dataclass(frozen=True)
class RecognitionResult:
    """Normalized posteriors and the optional automatic assignment."""

    posteriors: dict[int | str, float]
    best: int | str
    confidence: float
    assigned_user_id: int | None


def predictive_sigma(
    user: UserModel,
    timestamp: float,
    params: RecognitionParams = RecognitionParams(),
) -> float | None:
    """Return the capped predictive sigma, including drift and observation noise."""
    if user.mu_kg is None:
        return None
    sigma = max(user.sigma_kg or params.new_user_sigma_kg, params.sigma_floor_kg)
    days = _days_since(user.last_seen_ts, timestamp)
    variance = (
        sigma**2
        + params.drift_kg_per_sqrt_day**2 * days
        + params.observation_sigma_kg**2
    )
    return min(math.sqrt(variance), params.predictive_sigma_cap_kg)


def recognize(
    weight_kg: float,
    timestamp: float,
    users: list[UserModel] | tuple[UserModel, ...],
    params: RecognitionParams = RecognitionParams(),
) -> RecognitionResult:
    """Compute user-plus-unknown posteriors and the conservative auto-assignment."""
    log_weights: dict[int | str, float] = {}
    for user in users:
        sigma = predictive_sigma(user, timestamp, params)
        if user.mu_kg is None or sigma is None:
            log_weights[user.id] = -math.inf
            continue
        residual = weight_kg - user.mu_kg
        log_density = (
            -0.5 * math.log(2.0 * math.pi * sigma**2)
            - 0.5 * residual**2 / sigma**2
        )
        log_weights[user.id] = math.log(user.weigh_count + 1.0) + log_density

    if params.unknown_min_kg <= weight_kg <= params.unknown_max_kg:
        unknown_density = 1.0 / (params.unknown_max_kg - params.unknown_min_kg)
        log_weights[UNKNOWN] = (
            math.log(params.unknown_pseudo_count) + math.log(unknown_density)
        )
    else:
        log_weights[UNKNOWN] = -math.inf

    finite = [value for value in log_weights.values() if math.isfinite(value)]
    if not finite:
        return RecognitionResult({UNKNOWN: 1.0}, UNKNOWN, 1.0, None)
    peak = max(finite)
    weights = {
        key: (math.exp(value - peak) if math.isfinite(value) else 0.0)
        for key, value in log_weights.items()
    }
    total = sum(weights.values())
    posteriors = {key: value / total for key, value in weights.items()}
    best = max(posteriors, key=posteriors.__getitem__)
    confidence = posteriors[best]
    assigned = (
        int(best)
        if best != UNKNOWN and confidence >= params.auto_assign_threshold
        else None
    )
    return RecognitionResult(posteriors, best, confidence, assigned)


def update_belief(
    user: UserModel,
    weight_kg: float,
    timestamp: float,
    params: RecognitionParams = RecognitionParams(),
) -> UserModel:
    """Apply one Kalman update, or seed an uninitialized user at 2 kg sigma."""
    if user.mu_kg is None:
        return UserModel(
            id=user.id,
            name=user.name,
            mu_kg=weight_kg,
            sigma_kg=params.new_user_sigma_kg,
            last_seen_ts=timestamp,
            weigh_count=user.weigh_count + 1,
        )

    sigma = max(user.sigma_kg or params.new_user_sigma_kg, params.sigma_floor_kg)
    days = _days_since(user.last_seen_ts, timestamp)
    prior_variance = sigma**2 + params.drift_kg_per_sqrt_day**2 * days
    observation_variance = params.observation_sigma_kg**2
    gain = prior_variance / (prior_variance + observation_variance)
    mean = user.mu_kg + gain * (weight_kg - user.mu_kg)
    variance = (1.0 - gain) * prior_variance
    return UserModel(
        id=user.id,
        name=user.name,
        mu_kg=mean,
        sigma_kg=max(math.sqrt(variance), params.sigma_floor_kg),
        last_seen_ts=timestamp,
        weigh_count=user.weigh_count + 1,
    )


def _days_since(last_seen_ts: float | None, timestamp: float) -> float:
    if last_seen_ts is None:
        return 0.0
    return max(0.0, timestamp - last_seen_ts) / SECONDS_PER_DAY
