"""End-to-end tests for timeouts and transient Hive server failures.

A real aiohttp server on localhost stands in for beekeeper.hivehome.com and
answers each request with a scripted outcome: the fixture data, a hang past
the request timeout, a dropped connection, an HTTP 5xx or a truncated body.
The session is driven the way the Home Assistant fork's entities drive it:
every 15 seconds each entity calls ``update_data()`` and then ``get_climate()``
or ``get_water_heater()``. Time is a fake clock, so polls, retries and the
scan interval are checked to the second without waiting.
"""

# pylint: disable=protected-access,redefined-outer-name

import asyncio
import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer

from apyhiveapi import Hive
from apyhiveapi.exceptions import HiveConnectionError
from apyhiveapi.hive import POLL_RETRY_DELAY

_DATA = json.loads(
    (Path(__file__).parents[2] / "src/apyhiveapi/data/data.json").read_text()
)
_HEATING_ID = "heating-0000-0000-0000-000000000001"
_HOTWATER_ID = "hotwater-0000-0000-0000-000000000001"
_START = datetime(2026, 1, 1, 12, tzinfo=UTC)
# How often each Home Assistant entity calls update_data (the fork's
# SCAN_INTERVAL) and the library's scan interval (the fork's default option).
_ENTITY_INTERVAL = timedelta(seconds=15)
_SCAN_INTERVAL = 120
# Short enough to keep the suite fast, long enough not to trip on a slow CI box.
_TEST_TIMEOUT = ClientTimeout(total=0.5, connect=0.5)
_COGNITO_RESULT = {
    "AuthenticationResult": {
        "IdToken": "id-token",
        "RefreshToken": "refresh-token",
        "AccessToken": "access-token",
        "ExpiresIn": 3600,
    }
}
# The outcomes for the requests of one failed poll or command. aiohttp
# resends a GET once by itself when a kept-alive connection is dropped, so a
# dropped poll drops that resend too (see TestDroppedConnection).
_FAULTS = [
    pytest.param(["timeout"], id="timeout"),
    pytest.param(["disconnect", "disconnect"], id="dropped-connection"),
    pytest.param([500], id="http-500"),
    pytest.param([502], id="http-502"),
    pytest.param([503], id="http-503"),
    pytest.param([504], id="http-504"),
    pytest.param(["truncated"], id="truncated-body"),
]
_POLL_WARNING = "Could not poll Hive, keeping the last known state"


class _Clock:
    """A settable clock patched in for ``datetime`` in ``apyhiveapi.hive``."""

    def __init__(self):
        self.now = _START

    def advance(self, delta):
        self.now += delta

    @property
    def elapsed(self):
        return int((self.now - _START).total_seconds())


class _FakeHive:
    """Scripted stand-in for the Hive API.

    Each request takes the next outcome from its queue and answers with the
    current ``nodes`` once the queue is empty.
    """

    def __init__(self, clock):
        self.clock = clock
        self.nodes = copy.deepcopy(_DATA)
        self.poll_outcomes = []
        self.command_outcomes = []
        self.polls = []
        self.commands = []
        self._release = asyncio.Event()
        app = web.Application()
        app.router.add_get("/1.0/nodes/all", self._get_all)
        app.router.add_post("/1.0/nodes/{type}/{id}", self._set_state)
        self.server = TestServer(app)

    def set_temperature(self, product_id, value):
        product = next(p for p in self.nodes["products"] if p["id"] == product_id)
        product["props"]["temperature"] = value

    async def _get_all(self, request):
        self.polls.append(self.clock.elapsed)
        outcome = self.poll_outcomes.pop(0) if self.poll_outcomes else "ok"
        return await self._respond(request, outcome, json.dumps(self.nodes))

    async def _set_state(self, request):
        self.commands.append(await request.json())
        outcome = self.command_outcomes.pop(0) if self.command_outcomes else "ok"
        return await self._respond(request, outcome, "{}")

    async def _respond(self, request, outcome, body):
        if outcome == "timeout":
            # Hang until the test ends; the client gives up first.
            await self._release.wait()
        if outcome == "disconnect":
            request.transport.close()
        if isinstance(outcome, int):
            return web.Response(status=outcome, text="Service Unavailable")
        if outcome == "truncated":
            return web.Response(text=body[: len(body) // 2])
        return web.Response(text=body)

    async def close(self):
        self._release.set()
        await self.server.close()


@pytest.fixture
def clock():
    """The fake clock the session reads."""
    fake = _Clock()

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake.now

    with patch("apyhiveapi.hive.datetime", _FakeDatetime):
        yield fake


@pytest.fixture
async def fake_hive(clock, monkeypatch):
    """A running fake Hive API with the library pointed at it."""
    server = _FakeHive(clock)
    await server.server.start_server()
    base = str(server.server.make_url("/1.0"))
    monkeypatch.setattr("apyhiveapi.api._BASE_URL", base)
    monkeypatch.setattr("apyhiveapi.api._ALL_NODES_URL", f"{base}/nodes/all")
    monkeypatch.setattr("apyhiveapi.api._TIMEOUT", _TEST_TIMEOUT)
    yield server
    await server.close()


@pytest.fixture
async def websession():
    """The shared aiohttp session Home Assistant passes to the library."""
    async with ClientSession() as session:
        yield session


def _make_hive(websession):
    """Return a live-mode Hive whose Cognito calls are mocked."""
    hive = Hive(websession)
    hive.auth.refresh_token = AsyncMock(return_value=_COGNITO_RESULT)
    hive.login = AsyncMock()
    return hive


async def _start(hive):
    return await hive.start_session(
        {
            "username": "user@example.com",
            "password": "password",  # pragma: allowlist secret
            "tokens": {"token": "id-token", "refreshToken": "refresh-token"},
            "options": {"scan_interval": _SCAN_INTERVAL},
        }
    )


@pytest.fixture
async def session(fake_hive, websession):
    """A started live-mode session polling the fake Hive API."""
    hive = _make_hive(websession)
    await _start(hive)
    assert fake_hive.polls == [0]
    return hive


def _entities(hive):
    return [*hive.device_list["climate"], *hive.device_list["water_heater"]]


def _device(hive, hive_id):
    return next(d for d in _entities(hive) if d.hive_id == hive_id)


async def _entity_updates(hive):
    """Run one async_update for every entity, as the fork's entities do."""
    results = []
    for device in hive.device_list["climate"]:
        results.append(await hive.update_data())
        await hive.heating.get_climate(device)
    for device in hive.device_list["water_heater"]:
        results.append(await hive.update_data())
        await hive.hotwater.get_water_heater(device)
    return results


async def _run_for(hive, clock, duration):
    """Run the entity updates every 15 seconds for *duration*."""
    end = clock.now + duration
    while clock.now < end:
        clock.advance(_ENTITY_INTERVAL)
        await _entity_updates(hive)


def _poll_times(fake_hive):
    """Seconds after the start at which polls reached Hive, resends merged."""
    return sorted(set(fake_hive.polls))


def _statuses(hive):
    return {d.hive_id: (d.status, d.device_data.get("online")) for d in _entities(hive)}


def _poll_warnings(caplog):
    return [r for r in caplog.records if r.getMessage().startswith(_POLL_WARNING)]


class TestSinglePollFailure:
    """One poll fails; the session keeps its state and retries soon after."""

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_the_poll_reports_false_and_raises_nothing(
        self, session, fake_hive, clock, fault
    ):
        fake_hive.poll_outcomes = list(fault)
        clock.advance(timedelta(seconds=_SCAN_INTERVAL))
        assert await session.update_data() is False
        assert _poll_times(fake_hive) == [0, _SCAN_INTERVAL]

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_the_last_known_status_is_kept(
        self, session, fake_hive, clock, fault
    ):
        await _entity_updates(session)
        before = _statuses(session)
        fake_hive.poll_outcomes = list(fault)
        fake_hive.set_temperature(_HEATING_ID, 23.5)
        clock.advance(timedelta(seconds=_SCAN_INTERVAL))
        await _entity_updates(session)
        assert _statuses(session) == before
        heating = _device(session, _HEATING_ID)
        assert heating.status["current_temperature"] == 19.2
        assert heating.device_data["online"] is True

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_one_warning_is_logged(
        self, session, fake_hive, clock, caplog, fault
    ):
        fake_hive.poll_outcomes = list(fault)
        clock.advance(timedelta(seconds=_SCAN_INTERVAL))
        with caplog.at_level(logging.WARNING, logger="apyhiveapi"):
            await _entity_updates(session)
        assert len(_poll_warnings(caplog)) == 1
        assert all(r.exc_info is None for r in caplog.records)

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_the_retry_comes_after_the_retry_delay(
        self, session, fake_hive, clock, fault
    ):
        fake_hive.poll_outcomes = list(fault)
        clock.advance(timedelta(seconds=_SCAN_INTERVAL))
        await session.update_data()
        clock.advance(POLL_RETRY_DELAY - timedelta(seconds=1))
        assert await session.update_data() is False
        assert _poll_times(fake_hive) == [0, _SCAN_INTERVAL]
        clock.advance(timedelta(seconds=1))
        assert await session.update_data() is True
        assert _poll_times(fake_hive) == [0, _SCAN_INTERVAL, _SCAN_INTERVAL + 30]

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_new_data_is_picked_up_by_the_retry(
        self, session, fake_hive, clock, fault
    ):
        fake_hive.poll_outcomes = list(fault)
        fake_hive.set_temperature(_HEATING_ID, 23.5)
        clock.advance(timedelta(seconds=_SCAN_INTERVAL))
        await _entity_updates(session)
        clock.advance(POLL_RETRY_DELAY)
        await _entity_updates(session)
        assert _device(session, _HEATING_ID).status["current_temperature"] == 23.5

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_no_login_is_attempted(self, session, fake_hive, clock, fault):
        fake_hive.poll_outcomes = list(fault)
        clock.advance(timedelta(seconds=_SCAN_INTERVAL))
        await session.update_data()
        session.login.assert_not_awaited()
        session.auth.refresh_token.assert_awaited_once()


class TestIntermittentFailures:
    """Failures mixed with successes over a run of entity updates."""

    async def test_polls_follow_the_retry_and_scan_schedule(
        self, session, fake_hive, clock
    ):
        # The poll at 120 s times out, its retry at 150 s gets a 503, the
        # retry at 180 s succeeds and the next poll is a full interval later.
        fake_hive.poll_outcomes = ["timeout", 503]
        await _run_for(session, clock, timedelta(minutes=6))
        assert fake_hive.polls == [0, 120, 150, 180, 300]

    async def test_the_status_changes_only_when_a_poll_succeeds(
        self, session, fake_hive, clock
    ):
        fake_hive.poll_outcomes = ["timeout", 503]
        fake_hive.set_temperature(_HEATING_ID, 20.5)
        heating = _device(session, _HEATING_ID)
        seen = {}
        end = clock.now + timedelta(minutes=4)
        while clock.now < end:
            clock.advance(_ENTITY_INTERVAL)
            await _entity_updates(session)
            seen[clock.elapsed] = heating.status["current_temperature"]
        assert {t for t, temp in seen.items() if temp == 19.2} == set(
            range(15, 180, 15)
        )
        assert {t for t, temp in seen.items() if temp == 20.5} == set(
            range(180, 255, 15)
        )

    async def test_alternating_failures_never_blank_the_status(
        self, session, fake_hive, clock
    ):
        fake_hive.poll_outcomes = [
            "timeout",
            "ok",
            502,
            "ok",
            "disconnect",
            "disconnect",
            "ok",
        ]
        end = clock.now + timedelta(minutes=15)
        while clock.now < end:
            clock.advance(_ENTITY_INTERVAL)
            await _entity_updates(session)
            for device in _entities(session):
                assert None not in device.status.values()
                assert device.device_data["online"] is True
        assert not fake_hive.poll_outcomes


class TestSustainedOutage:
    """Hive keeps failing for several minutes."""

    async def test_polls_are_retried_every_retry_delay_not_every_update(
        self, session, fake_hive, clock
    ):
        fake_hive.poll_outcomes = [503] * 20
        await _run_for(session, clock, timedelta(minutes=5))
        assert fake_hive.polls == [0, 120, 150, 180, 210, 240, 270, 300]

    async def test_one_warning_is_logged_per_failed_poll(
        self, session, fake_hive, clock, caplog
    ):
        fake_hive.poll_outcomes = [503] * 20
        with caplog.at_level(logging.WARNING, logger="apyhiveapi"):
            await _run_for(session, clock, timedelta(minutes=5))
        assert len(_poll_warnings(caplog)) == len(fake_hive.polls) - 1

    async def test_the_last_known_status_is_reported_throughout(
        self, session, fake_hive, clock
    ):
        # The library has no notion of data going stale: the entities keep
        # the last values Hive sent, and stay available, for the whole outage.
        await _entity_updates(session)
        before = _statuses(session)
        fake_hive.poll_outcomes = [503] * 20
        await _run_for(session, clock, timedelta(minutes=10))
        assert _statuses(session) == before

    async def test_the_session_recovers_when_hive_does(self, session, fake_hive, clock):
        fake_hive.poll_outcomes = ["timeout", 503, 504, "disconnect", "disconnect"]
        fake_hive.set_temperature(_HEATING_ID, 22.0)
        await _run_for(session, clock, timedelta(minutes=5))
        assert _poll_times(fake_hive) == [0, 120, 150, 180, 210, 240]
        assert _device(session, _HEATING_ID).status["current_temperature"] == 22.0
        session.login.assert_not_awaited()


class TestDroppedConnection:
    """Hive drops a kept-alive connection once."""

    async def test_aiohttp_resends_the_poll_and_it_succeeds(
        self, session, fake_hive, clock, caplog
    ):
        fake_hive.poll_outcomes = ["disconnect"]
        fake_hive.set_temperature(_HEATING_ID, 21.5)
        clock.advance(timedelta(seconds=_SCAN_INTERVAL))
        with caplog.at_level(logging.WARNING, logger="apyhiveapi"):
            await _entity_updates(session)
        assert fake_hive.polls == [0, _SCAN_INTERVAL, _SCAN_INTERVAL]
        assert _device(session, _HEATING_ID).status["current_temperature"] == 21.5
        assert not _poll_warnings(caplog)

    async def test_a_command_is_not_resent(self, session, fake_hive):
        fake_hive.command_outcomes = ["disconnect"]
        heating = _device(session, _HEATING_ID)
        with pytest.raises(HiveConnectionError):
            await session.heating.set_target_temperature(heating, 21.0)
        assert fake_hive.commands == [{"target": 21.0}]


class TestSlowServer:
    """Entities updating while a poll waits on a slow Hive."""

    async def test_other_entities_do_not_wait_or_poll_again(
        self, session, fake_hive, clock
    ):
        fake_hive.poll_outcomes = ["timeout"]
        clock.advance(timedelta(seconds=_SCAN_INTERVAL))
        loop = asyncio.get_running_loop()
        first = asyncio.create_task(session.update_data())
        await asyncio.sleep(0.05)
        started = loop.time()
        others = await asyncio.gather(session.update_data(), session.update_data())
        assert loop.time() - started < 0.1
        assert others == [False, False]
        assert await first is False
        assert fake_hive.polls == [0, _SCAN_INTERVAL]

    async def test_a_hung_request_gives_up_at_the_timeout(
        self, session, fake_hive, clock
    ):
        fake_hive.poll_outcomes = ["timeout"]
        clock.advance(timedelta(seconds=_SCAN_INTERVAL))
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await session.update_data() is False
        assert loop.time() - started < _TEST_TIMEOUT.total + 0.5


class TestSessionStart:
    """Transient failures while Home Assistant sets the integration up."""

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_start_session_raises_a_connection_error(
        self, fake_hive, websession, fault
    ):
        # The fork maps HiveConnectionError to ConfigEntryNotReady, so Home
        # Assistant retries the setup with backoff.
        fake_hive.poll_outcomes = list(fault)
        hive = _make_hive(websession)
        with pytest.raises(HiveConnectionError):
            await _start(hive)
        hive.login.assert_not_awaited()

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_a_later_setup_attempt_succeeds(self, fake_hive, websession, fault):
        fake_hive.poll_outcomes = list(fault)
        with pytest.raises(HiveConnectionError):
            await _start(_make_hive(websession))
        devices = await _start(_make_hive(websession))
        assert {d.hive_id for d in devices["climate"]} >= {_HEATING_ID}
        assert [d.hive_id for d in devices["water_heater"]] == [_HOTWATER_ID]


class TestCognitoUnreachable:
    """The token refresh before a poll cannot reach Cognito."""

    async def test_the_poll_is_skipped_and_retried(self, session, fake_hive, clock):
        session.auth.refresh_token.side_effect = [
            HiveConnectionError("Cognito timed out"),
            _COGNITO_RESULT,
        ]
        # Move past 90% of the token lifetime so the next poll refreshes.
        clock.advance(timedelta(minutes=55))
        await _entity_updates(session)
        assert fake_hive.polls == [0]
        clock.advance(POLL_RETRY_DELAY)
        await _entity_updates(session)
        assert fake_hive.polls == [0, 3330]
        session.login.assert_not_awaited()


class TestCommandsDuringFailures:
    """Commands sent while Hive is failing."""

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_a_failed_command_raises_a_connection_error(
        self, session, fake_hive, fault
    ):
        # The fork turns any HiveError from a command into HomeAssistantError.
        fake_hive.command_outcomes = list(fault)
        heating = _device(session, _HEATING_ID)
        await session.heating.get_climate(heating)
        with pytest.raises(HiveConnectionError):
            await session.heating.set_target_temperature(heating, 21.0)
        assert fake_hive.polls == [0]
        session.login.assert_not_awaited()

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_a_failed_poll_after_a_command_does_not_raise(
        self, session, fake_hive, fault, caplog
    ):
        fake_hive.poll_outcomes = list(fault)
        heating = _device(session, _HEATING_ID)
        with caplog.at_level(logging.WARNING, logger="apyhiveapi"):
            await session.heating.set_target_temperature(heating, 21.0)
        assert fake_hive.commands == [{"target": 21.0}]
        assert _poll_times(fake_hive) == [0]
        assert len(_poll_warnings(caplog)) == 1
