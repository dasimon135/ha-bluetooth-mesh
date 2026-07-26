"""Unit tests for provision_and_toggle's pure logic.

The hardware flow itself is untestable here, but the state persistence (the
semi-brick risk: a lost SEQ means the node silently ignores us), the TX pump
ordering/failure reporting, device selection, and CLI validation are plain
Python — pin them.
"""

import asyncio
import json
from types import SimpleNamespace

import provision_and_toggle as pt
import pytest

from btmesh.bearer import BearerError

# ------------------------------------------------------------------- state


def test_state_create_then_load_round_trip(tmp_path):
    path = tmp_path / "state.json"
    created = pt.load_or_create_state(path)
    assert path.exists()
    loaded = pt.load_or_create_state(path)
    assert loaded == created
    assert len(bytes.fromhex(loaded["netkey"])) == 16
    assert len(bytes.fromhex(loaded["appkey"])) == 16
    assert loaded["provisioner_addr"] == 0x0001
    assert loaded["next_unicast"] == 0x0002
    assert loaded["seq"] == 0
    assert loaded["devices"] == {}


def test_save_state_is_atomic_and_overwrites(tmp_path):
    path = tmp_path / "state.json"
    state = pt.load_or_create_state(path)
    state["seq"] = 1234
    pt.save_state(path, state)
    assert json.loads(path.read_text())["seq"] == 1234
    # The tmp file from the write+replace dance must not linger.
    leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []


@pytest.mark.parametrize(
    "corruption",
    [
        "{not json",                                      # unparseable
        json.dumps({"netkey": "00" * 16}),                # missing fields
        None,                                             # seq wrong type (below)
        "short-netkey",                                   # bad key hex (below)
    ],
)
def test_load_corrupt_state_raises_phase0_failure(tmp_path, corruption):
    path = tmp_path / "state.json"
    good = pt.load_or_create_state(path)
    if corruption == "short-netkey":
        good["netkey"] = "abcd"
        path.write_text(json.dumps(good))
    elif corruption is None:
        good["seq"] = "42"  # string, not int
        path.write_text(json.dumps(good))
    else:
        path.write_text(corruption)
    with pytest.raises(pt.Phase0Failure) as excinfo:
        pt.load_or_create_state(path)
    assert excinfo.value.phase == "startup"


# ------------------------------------------------------------------- pump


class RecordingBearer:
    def __init__(self):
        self.sent = []

    async def send(self, msg_type, pdu):
        self.sent.append((msg_type, pdu))


class FailingBearer:
    async def send(self, msg_type, pdu):
        raise BearerError("proxy link dropped")


async def _wait_for(predicate, timeout=1.0):
    for _ in range(int(timeout / 0.001)):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition not reached in time")


async def test_pump_preserves_order():
    bearer = RecordingBearer()
    pump = pt.BearerPump(bearer, 0x03)
    for i in range(5):
        pump.put(bytes([i]))
    pump.start()
    await _wait_for(lambda: len(bearer.sent) == 5)
    await pump.stop()
    assert bearer.sent == [(0x03, bytes([i])) for i in range(5)]
    assert pump.failure is None


async def test_pump_records_failure_and_calls_on_error():
    pump = pt.BearerPump(FailingBearer(), 0x00)
    errors = []
    pump.on_error = errors.append
    pump.put(b"\x00")
    pump.start()
    await _wait_for(lambda: pump.failure is not None)
    await pump.stop()
    assert isinstance(pump.failure, BearerError)
    assert errors == [pump.failure]


def test_timeout_causes_prepends_pump_failure():
    pump = pt.BearerPump(RecordingBearer(), 0x00)
    assert pt._timeout_causes(pump) == pt.CONFIG_TIMEOUT_CAUSES

    pump.failure = BearerError("boom")
    causes = pt._timeout_causes(pump, ["extra cause"])
    assert "bearer TX pump died: boom" in causes[0]
    assert causes[1:] == pt.CONFIG_TIMEOUT_CAUSES + ["extra cause"]


# ------------------------------------------------------------- pick_device


def _state_with_devices():
    return {
        "devices": {
            "0x0002": {"device_key": "00" * 16, "uuid": "aa" * 16},
            "0x0003": {"device_key": "11" * 16, "uuid": "bb" * 16},
        }
    }


def test_pick_device_requires_a_provisioned_device():
    with pytest.raises(pt.Phase0Failure):
        pt.pick_device({"devices": {}}, SimpleNamespace(device_uuid=None))


def test_pick_device_defaults_to_most_recent():
    args = SimpleNamespace(device_uuid=None)
    assert pt.pick_device(_state_with_devices(), args) == 0x0003


def test_pick_device_by_uuid():
    args = SimpleNamespace(device_uuid="aa" * 16)
    assert pt.pick_device(_state_with_devices(), args) == 0x0002


def test_pick_device_unknown_uuid():
    args = SimpleNamespace(device_uuid="cc" * 16)
    with pytest.raises(pt.Phase0Failure):
        pt.pick_device(_state_with_devices(), args)


# -------------------------------------------------------------- parse_args


def test_parse_args_normalizes_device_uuid():
    args = pt.parse_args(
        ["--transport", "local", "--device-uuid", "AABBCCDD-EEFF-0011-2233-445566778899"]
    )
    assert args.device_uuid == "aabbccddeeff00112233445566778899"


@pytest.mark.parametrize(
    "argv",
    [
        ["--transport", "esphome"],  # missing --esphome-host
        ["--transport", "local", "--device-uuid", "abcd"],  # not 16 bytes
        ["--transport", "local", "--device-uuid", "zz" * 16],  # not hex
        ["--transport", "local", "--static-oob", ""],  # empty
        ["--transport", "local", "--static-oob", "00" * 17],  # too long
        ["--transport", "local", "--static-oob", "xyz"],  # not hex
    ],
)
def test_parse_args_rejects_bad_input(argv):
    with pytest.raises(SystemExit):
        pt.parse_args(argv)


def test_parse_args_accepts_static_oob():
    args = pt.parse_args(["--transport", "local", "--static-oob", "DEADBEEF"])
    assert args.static_oob == "DEADBEEF"
