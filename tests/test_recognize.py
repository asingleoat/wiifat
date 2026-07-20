import math

import pytest

from wiifat.recognize import (
    UNKNOWN,
    UserModel,
    predictive_sigma,
    recognize,
    update_belief,
)


DAY = 86_400.0


def user(mu, sigma=0.5, *, last_seen=0.0, count=10, user_id=1):
    return UserModel(user_id, f"User {user_id}", mu, sigma, last_seen, count)


def test_yesterdays_200_lb_owner_does_not_claim_140_lb_visitor():
    result = recognize(63.5, DAY, [user(90.7)])

    assert result.best == UNKNOWN
    assert result.assigned_user_id is None
    assert result.posteriors[UNKNOWN] > 0.99


def test_small_next_day_delta_auto_assigns_with_high_posterior():
    result = recognize(90.9, DAY, [user(90.7)])

    assert result.assigned_user_id == 1
    assert result.confidence >= 0.90


def test_predictive_sigma_widens_with_absence():
    model = user(80.0, sigma=0.5)

    tomorrow = predictive_sigma(model, DAY)
    next_year = predictive_sigma(model, 365 * DAY)

    assert tomorrow is not None and next_year is not None
    assert next_year > tomorrow
    assert next_year <= 8.0


def test_kalman_update_matches_closed_form():
    model = user(70.0, sigma=2.0, count=3)
    updated = update_belief(model, 72.0, DAY)
    prior_variance = 2.0**2 + 0.2**2
    gain = prior_variance / (prior_variance + 0.6**2)

    assert updated.mu_kg == pytest.approx(70.0 + gain * 2.0)
    assert updated.sigma_kg == pytest.approx(
        math.sqrt((1.0 - gain) * prior_variance)
    )
    assert updated.weigh_count == 4
    assert updated.last_seen_ts == DAY


def test_two_nearby_users_are_ambiguous_at_the_midpoint():
    result = recognize(
        71.0,
        DAY,
        [user(70.0, user_id=1), user(72.0, user_id=2)],
    )

    assert result.assigned_user_id is None
    assert result.confidence < 0.90
