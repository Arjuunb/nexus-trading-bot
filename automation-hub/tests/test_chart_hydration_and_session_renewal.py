"""Two things that failed quietly rather than loudly.

``live_state`` took the worker's lock without blocking and, when it lost that
race, built a brand-new empty engine and rendered it as though it were the live
one -- a chart showing a single candle and "0 closed candles" with nothing
saying anything was wrong.

The Supabase session cookie held an access token for thirty days while the
token inside it expired on the provider's schedule (an hour by default, less if
the project sets it lower). Nothing renewed it, so the dashboard demanded a
fresh sign-in mid-session while the refresh token was still valid.
"""
from __future__ import annotations

import inspect

import app as app_module
from services import price_action_lab, supabase_auth


def test_hydration_waits_briefly_rather_than_losing_the_race_outright():
    source = inspect.getsource(price_action_lab.PriceActionLabRuntime.live_state)
    assert "acquire(blocking=False)" not in source, (
        "A purely non-blocking acquire lost the lock whenever a tick was in "
        "flight and then rendered an empty engine as the live one.")
    assert "_HYDRATION_LOCK_WAIT_S" in source
    assert 0 < price_action_lab._HYDRATION_LOCK_WAIT_S <= 1.0, (
        "The wait must be bounded: long enough to win the ordinary race, "
        "short enough that it can never queue behind the worker's I/O.")


def test_a_detached_snapshot_says_so_instead_of_posing_as_live():
    source = inspect.getsource(price_action_lab.PriceActionLabRuntime.live_state)
    assert '"hydration"' in source
    assert '"source": "worker" if live else "detached"' in source, (
        "A caller must be able to tell an empty session from a snapshot the "
        "request could not reach; both rendered identically before.")
    # And the reason is specific rather than a bare flag.
    assert '"reason"' in source
    assert "worker busy" in source


def test_hydration_still_never_bootstraps_from_the_request_thread():
    """The original constraint holds: this path reads, it does not fetch."""
    source = inspect.getsource(price_action_lab.PriceActionLabRuntime.live_state)
    # Code only -- the docstring and comments in this method discuss
    # bootstrapping precisely because it must not happen here.
    code = "\n".join(line.split("#")[0] for line in source.splitlines())
    for forbidden in ("fetch_forward_bars(", "bootstrap(", "load_markets(",
                      "fetch_ohlcv(", "_bootstrap("):
        assert forbidden not in code, (
            f"live_state must not call {forbidden} on the request thread.")


def test_the_session_keeps_the_refresh_half():
    assert hasattr(app_module, "SUPABASE_REFRESH_COOKIE")
    assert app_module.SUPABASE_REFRESH_COOKIE != app_module.SUPABASE_COOKIE
    source = inspect.getsource(app_module.supabase_session)
    assert "SUPABASE_REFRESH_COOKIE" in source, (
        "Without somewhere to keep the refresh token the server cannot renew "
        "an expired access token, which is the whole bug.")


def test_a_renewal_endpoint_exists_and_rotates_both_tokens():
    assert hasattr(app_module, "supabase_refresh")
    source = inspect.getsource(app_module.supabase_refresh)
    assert "supabase_auth.refresh" in source
    # Supabase rotates the refresh token on every exchange; storing the old one
    # again would kill the session at the next renewal.
    assert 'data.get("refresh_token")' in source
    assert "set_cookie(SUPABASE_REFRESH_COOKIE" in source


def test_a_dead_refresh_token_clears_both_cookies():
    """A session that cannot recover must stop being retried with."""
    source = inspect.getsource(app_module.supabase_refresh)
    assert "delete_cookie(SUPABASE_COOKIE)" in source
    assert "delete_cookie(SUPABASE_REFRESH_COOKIE)" in source


def test_logout_clears_the_refresh_cookie_too():
    source = inspect.getsource(app_module)
    access = source.count("delete_cookie(SUPABASE_COOKIE)")
    refresh = source.count("delete_cookie(SUPABASE_REFRESH_COOKIE)")
    assert refresh >= access, (
        "Every path that drops the access cookie must drop the refresh half; "
        "leaving it behind keeps a usable credential after sign-out.")


def test_refresh_uses_the_provider_grant_and_demands_a_token():
    source = inspect.getsource(supabase_auth.SupabaseAuth.refresh)
    assert "grant_type=refresh_token" in source
    assert 'method="POST"' in source
    assert "A refresh token is required." in source
