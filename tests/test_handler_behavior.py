"""ModbusHandler behavior: regressions, documented contracts, and config validation."""

import csv
import io
import json
from math import inf
from unittest.mock import MagicMock

import pytest
from tests.helpers.modbus import (
    FakeModbusMaster,
    FakeModbusResult,
    handler_with_device,
    log_messages,
)

from modpoll.arg_parser import get_parser
from modpoll.modbus_task import Device, ModbusHandler, Poller, Reference
from modpoll.mqtt_task import MqttHandler


def _device_with_two_register_pollers():
    """Device with two holding-register pollers (same layout as modsim.csv)."""
    device = Device("dev", 1)
    p1 = Poller(device, 3, 0, 1, "BE_BE")
    p2 = Poller(device, 3, 10, 1, "BE_BE")
    ref1 = Reference(device, "r1", "0", "uint16", "r", None, None)
    ref2 = Reference(device, "r2", "10", "uint16", "r", None, None)
    p1.add_readable_reference(ref1)
    p2.add_readable_reference(ref2)
    device.add_reference_mapping(ref1)
    device.add_reference_mapping(ref2)
    device.pollerList = [p1, p2]
    return device, ref1, ref2


def _device_with_temp_ref(val=21):
    device = Device("dev", 1)
    device.pollSuccess = True
    ref = Reference(device, "temp", "0", "int16", "r", None, None)
    ref.val = val
    device.references = {"temp": ref}
    return device


# ---------------------------------------------------------------------------
# Fixed: pollSuccess OR semantics for multi-poller devices
# ---------------------------------------------------------------------------


def test_mqtt_publish_includes_device_when_first_poller_succeeds():
    device, ref1, ref2 = _device_with_two_register_pollers()
    master = FakeModbusMaster(registers=[0x1111], fail_addresses={10})

    for poller in device.pollerList:
        poller.poll(master)

    assert ref1.val == 0x1111
    assert ref2.val is None
    assert device.pollSuccess is True

    mqtt = MagicMock()
    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="modpoll/{{device_name}}",
    )
    handler.deviceList = [device]
    handler.publish_data()

    assert mqtt.publish_data_message.called
    payload = json.loads(mqtt.publish_data_message.call_args[0][1])
    assert payload["r1"] == 0x1111
    assert "r2" not in payload


def test_poll_success_true_when_any_poller_succeeds():
    device, ref1, ref2 = _device_with_two_register_pollers()
    master = FakeModbusMaster(registers=[0x1111], fail_addresses={10})

    for poller in device.pollerList:
        poller.poll(master)

    assert ref1.val == 0x1111
    assert device.pollSuccess is True


def test_modbus_unavailable_clears_poll_success_and_skips_mqtt():
    device = Device("dev", 1)
    ref = Reference(device, "r1", "0", "uint16", "r", None, None)
    ref.val = 42
    device.pollSuccess = True
    device.references = {"r1": ref}
    device.pollerList = []

    mqtt = MagicMock()
    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="t/{{device_name}}",
        no_output=True,
    )
    handler.deviceList = [device]

    handler.on_poll_unavailable()

    assert device.pollSuccess is False
    handler.publish_data()
    mqtt.publish_data_message.assert_not_called()


def test_publish_skips_device_when_poll_failed():
    device = Device("dev", 1)
    device.pollSuccess = False
    ref = Reference(device, "temp", "0", "uint16", "r", None, None)
    ref.val = 42
    device.references = {"temp": ref}

    mqtt = MagicMock()
    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="t/{{device_name}}",
    )
    handler.deviceList = [device]
    handler.publish_data()
    mqtt.publish_data_message.assert_not_called()


# ---------------------------------------------------------------------------
# Fixed: MQTT publish skips None reference values
# ---------------------------------------------------------------------------


def test_publish_data_uses_name_with_unit_by_default():
    device = Device("dev", 1)
    device.pollSuccess = True
    ref = Reference(device, "temp", "0", "int16", "r", "°C", 0.1)
    ref.val = 21.2
    device.references = {"temp": ref}

    mqtt = MagicMock()
    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="t/{{device_name}}",
    )
    handler.deviceList = [device]
    handler.publish_data()

    payload = json.loads(mqtt.publish_data_message.call_args[0][1])
    assert payload == {"temp|°C": 21.2}


def test_publish_data_mqtt_keys_name_only():
    device = Device("dev", 1)
    device.pollSuccess = True
    ref = Reference(device, "temp", "0", "int16", "r", "°C", 0.1)
    ref.val = 21.2
    device.references = {"temp": ref}

    mqtt = MagicMock()
    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="t/{{device_name}}",
        mqtt_keys="name-only",
    )
    handler.deviceList = [device]
    handler.publish_data()

    payload = json.loads(mqtt.publish_data_message.call_args[0][1])
    assert payload == {"temp": 21.2}


def test_mqtt_keys_name_only_cli_option():
    args = get_parser().parse_args(
        [
            "--config",
            "dummy.csv",
            "--tcp",
            "127.0.0.1",
            "--mqtt-keys",
            "name-only",
        ]
    )
    assert args.mqtt_keys == "name-only"


def test_mqtt_retain_cli_option():
    args = get_parser().parse_args(
        [
            "--config",
            "dummy.csv",
            "--tcp",
            "127.0.0.1",
            "--mqtt-retain",
        ]
    )
    assert args.mqtt_retain is True


@pytest.mark.parametrize("mqtt_single_publish", [False, True])
def test_publish_data_uses_mqtt_handler_publish_data_message(mqtt_single_publish):
    mqtt = MagicMock()
    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="t/{{device_name}}",
        mqtt_single_publish=mqtt_single_publish,
    )
    handler.deviceList = [_device_with_temp_ref()]
    handler.publish_data()

    assert mqtt.publish_data_message.called
    mqtt.publish.assert_not_called()


def test_publish_diagnostics_uses_publish_not_data_message():
    device = _device_with_temp_ref()
    device.pollCount = 3
    device.errorCount = 0

    mqtt = MagicMock()
    mqtt.retain_data_publishes = False
    handler = ModbusHandler(
        MagicMock(),
        "/path/to/meter.csv",
        mqtt_handler=mqtt,
        mqtt_diagnostics_topic_pattern="t/{{device_name}}/diag",
    )
    handler.deviceList = [device]
    handler.publish_diagnostics()

    mqtt.publish.assert_called_once()
    mqtt.publish_data_message.assert_not_called()
    assert mqtt.publish.call_args.kwargs.get("retain") is False
    payload = json.loads(mqtt.publish.call_args[0][1])
    assert payload["poll_count"] == 3
    assert payload["error_count"] == 0
    assert payload["last_poll_success"] is True
    assert payload["get_count"] == 0
    assert payload["get_errors"] == 0
    assert payload["get_success"] == 0
    assert payload["get_unknown_refs"] == 0
    assert payload["get_read_errors"] == 0
    assert payload["set_count"] == 0
    assert payload["set_errors"] == 0
    assert payload["set_success"] == 0
    assert payload["set_unknown_refs"] == 0
    assert payload["config_source"] == "/path/to/meter.csv"


def test_publish_diagnostics_uses_mqtt_retain_when_enabled():
    device = _device_with_temp_ref()

    mqtt = MagicMock()
    mqtt.retain_data_publishes = True
    handler = ModbusHandler(
        MagicMock(),
        "config.csv",
        mqtt_handler=mqtt,
        mqtt_diagnostics_topic_pattern="t/{{device_name}}/diag",
    )
    handler.deviceList = [device]
    handler.publish_diagnostics()

    assert mqtt.publish.call_args.kwargs.get("retain") is True


def test_publish_data_omits_null_reference_values():
    device = Device("dev", 1)
    device.pollSuccess = True
    good = Reference(device, "good", "0", "uint16", "r", None, None)
    good.val = 42
    bad = Reference(device, "bad", "10", "uint16", "r", None, None)
    bad.val = None
    device.references = {"good": good, "bad": bad}

    mqtt = MagicMock()
    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="t/{{device_name}}",
    )
    handler.deviceList = [device]
    handler.publish_data()

    payload = json.loads(mqtt.publish_data_message.call_args[0][1])
    assert payload == {"good": 42}


# ---------------------------------------------------------------------------
# Fixed: --autoremove disables poller after 3 consecutive failures
# ---------------------------------------------------------------------------


def test_autoremove_cli_flag_exists():
    args = get_parser().parse_args(
        ["--config", "dummy.csv", "--tcp", "127.0.0.1", "--autoremove"]
    )
    assert args.autoremove is True


def test_autoremove_disables_poller_after_three_failures():
    device, _, _ = _device_with_two_register_pollers()
    master = FakeModbusMaster(registers=[0x1111], fail_addresses={10})

    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        autoremove=True,
        no_output=True,
    )
    handler.deviceList = [device]
    handler.modbus_client = master

    for _ in range(3):
        handler.poll()

    assert device.pollerList[0].disabled is False
    assert device.pollerList[1].disabled is True


def test_autoremove_without_flag_keeps_poller_enabled():
    device = Device("dev", 1)
    poller = Poller(device, 3, 10, 1, "BE_BE")
    device.pollerList = [poller]

    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        autoremove=False,
        no_output=True,
    )
    handler.deviceList = [device]
    handler.modbus_client = MagicMock()
    handler.modbus_client.read_holding_registers = (
        lambda *args, **kwargs: FakeModbusResult(error=True)
    )

    for _ in range(5):
        handler.poll()

    assert poller.disabled is False
    assert poller.failcounter == 5


# ---------------------------------------------------------------------------
# Fixed: bool8 on holding registers decodes 16-bit word bits
# ---------------------------------------------------------------------------


def test_bool8_on_holding_register_should_decode_low_byte_0x00ff():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "flags", "0", "bool8", "r", None, None)
    poller.add_readable_reference(ref)

    master = FakeModbusMaster(registers=[0x00FF])
    assert poller.poll(master) is True
    assert ref.val == [True] * 8


def test_bool8_on_holding_register_reads_low_byte_bits_be_be():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "flags", "0", "bool8", "r", None, None)
    poller.add_readable_reference(ref)
    poller.poll(FakeModbusMaster(registers=[0x00FF]))
    assert ref.val == [True] * 8


def test_bool8_on_holding_register_respects_endian_configuration():
    device = Device("dev", 1)
    poller_be = Poller(device, 3, 0, 1, "BE_BE")
    ref_be = Reference(device, "flags_be", "0", "bool8", "r", None, None)
    poller_be.add_readable_reference(ref_be)
    poller_be.poll(FakeModbusMaster(registers=[0x00FF]))
    assert ref_be.val == [True] * 8

    poller_le = Poller(device, 3, 0, 1, "LE_BE")
    ref_le = Reference(device, "flags_le", "0", "bool8", "r", None, None)
    poller_le.add_readable_reference(ref_le)
    poller_le.poll(FakeModbusMaster(registers=[0x00FF]))
    assert ref_le.val == [False] * 8


def test_bool8_on_coils_and_discrete_inputs_works():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 8, "BE_BE")
    ref = Reference(device, "coil01-08", "0", "bool8", "r", None, None)
    poller.add_readable_reference(ref)
    poller.poll(FakeModbusMaster(bits=[True] * 8))
    assert ref.val == [True] * 8


# ---------------------------------------------------------------------------
# Documented limitations (passing tests lock current behaviour)
# ---------------------------------------------------------------------------


def test_coil_bool_reads_single_coil_at_modbus_address():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 8, "BE_BE")
    ref = Reference(device, "coil0", "0", "bool", "r", None, None)
    poller.add_readable_reference(ref)
    poller.poll(FakeModbusMaster(bits=[True] + [False] * 7))
    assert ref.val is True


def test_reference_scale_zero_is_silently_ignored():
    ref = Reference(Device("dev", 1), "scaled", "0", "uint16", "r", None, 0)
    ref.update_value(100)
    assert ref.val == 100


def test_json_dumps_allows_nan_which_is_invalid_json():
    dumped = json.dumps({"temperature": float("nan")})
    assert dumped == '{"temperature": NaN}'


def test_mqtt_connect_typeerror_returns_false():
    handler = MqttHandler(
        name="test",
        host="broker.local",
        port=1883,
        user=None,
        password=None,
        clientid="id",
        qos=0,
        mqtt_version="3.1.1",
    )
    assert handler.setup()

    def raise_type_error(**kwargs):
        raise TypeError("unexpected keyword argument")

    handler.mqtt_client.loop_start = lambda: None
    handler.mqtt_client.connect_async = raise_type_error

    assert handler.connect() is False


def test_bool16_check_sanity_requires_16_bits():
    device = Device("dev", 1)
    ref = Reference(device, "flags", "1", "bool16", "r", None, None)
    assert ref.check_sanity(0, 1, fc=1) is False
    ref_ok = Reference(device, "flags_ok", "0", "bool16", "r", None, None)
    assert ref_ok.check_sanity(0, 16, fc=1) is True


def test_handler_propagates_oserror_on_read_and_marks_failure():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 8, "BE_BE")
    ref = Reference(device, "coil0", "0", "bool", "r", None, None)
    ref.val = True
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"coil0": ref}

    class MasterRaisesOSError:
        def read_coils(self, *args, **kwargs):
            raise OSError("connection reset")

    handler = ModbusHandler(MasterRaisesOSError(), "dummy.csv", no_output=True)
    handler.deviceList = [device]

    with pytest.raises(OSError, match="connection reset"):
        handler.poll()

    assert device.pollSuccess is False
    assert device.pollCount == 1
    assert device.errorCount == 1
    assert ref.val is None


def test_poller_handles_none_result():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 8, "BE_BE")
    ref = Reference(device, "coil0", "0", "bool", "r", None, None)
    poller.add_readable_reference(ref)

    class MasterReturnsNone:
        def read_coils(self, *args, **kwargs):
            return None

    assert poller.poll(MasterReturnsNone()) is False
    assert ref.val is None


def test_autoremove_on_poll_unavailable():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    device.pollerList = [poller]

    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        autoremove=True,
        no_output=True,
    )
    handler.deviceList = [device]

    for _ in range(3):
        handler.on_poll_unavailable()

    assert poller.disabled is True


def test_export_omits_non_finite_floats(tmp_path):
    device = Device("dev", 1)
    good = Reference(device, "good", "0", "float32", "r", None, None)
    good.val = 1.5
    bad = Reference(device, "nan", "2", "float32", "r", None, None)
    bad.val = float("nan")
    device.references = {"good": good, "nan": bad}

    handler = ModbusHandler(MagicMock(), "dummy.csv")
    handler.deviceList = [device]
    export_file = tmp_path / "out.json"
    handler.export(str(export_file))

    data = json.loads(export_file.read_text())
    assert data == {"dev": {"good": 1.5}}


def test_mqtt_single_publishes_per_reference_topics():
    device = Device("dev", 1)
    device.pollSuccess = True
    bool_ref = Reference(device, "flag", "0", "bool", "r", None, None)
    bool_ref.val = True
    list_ref = Reference(device, "flags", "1", "bool8", "r", None, None)
    list_ref.val = [True, False, True, False, False, False, False, False]
    device.references = {"flag": bool_ref, "flags": list_ref}

    mqtt = MagicMock()
    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="modpoll/{{device_name}}/data",
        mqtt_single_publish=True,
    )
    handler.deviceList = [device]
    handler.publish_data()

    published = {
        call[0][0]: call[0][1] for call in mqtt.publish_data_message.call_args_list
    }
    assert published["modpoll/dev/data/flag"] == "true"
    assert published["modpoll/dev/data/flags/0"] == "true"
    assert published["modpoll/dev/data/flags/7"] == "false"


def test_publish_data_omits_non_finite_floats():
    device = Device("dev", 1)
    device.pollSuccess = True
    good = Reference(device, "good", "0", "float32", "r", None, None)
    good.val = 1.5
    nan_ref = Reference(device, "nan", "2", "float32", "r", None, None)
    nan_ref.val = float("nan")
    inf_ref = Reference(device, "inf", "4", "float32", "r", None, None)
    inf_ref.val = inf
    device.references = {"good": good, "nan": nan_ref, "inf": inf_ref}

    mqtt = MagicMock()
    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="t/{{device_name}}",
    )
    handler.deviceList = [device]
    handler.publish_data()

    payload = json.loads(mqtt.publish_data_message.call_args[0][1])
    assert payload == {"good": 1.5}


# ---------------------------------------------------------------------------
# Documented write contracts
# ---------------------------------------------------------------------------


def _device_with_bool8_coil_ref():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 16, "BE_BE")
    ref = Reference(device, "coil01-08", "0", "bool8", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"coil01-08": ref}
    return device


def test_write_bool8_rejects_scalar_boolean():
    device = _device_with_bool8_coil_ref()
    master = FakeModbusMaster(coils=[False] * 16)
    handler = handler_with_device(device, master)
    assert handler.write_reference("dev", "coil01-08", True) is False
    assert master.writes == []


def test_write_bool8_accepts_list_of_eight():
    device = _device_with_bool8_coil_ref()
    master = FakeModbusMaster(coils=[False] * 16)
    handler = handler_with_device(device, master)
    flags = [True, False, True, False, False, False, False, False]
    assert handler.write_reference("dev", "coil01-08", flags) is True
    assert master.writes[0][0] == "coils"
    assert master.writes[0][2][0:8] == flags


def test_write_rejects_discrete_input():
    device = Device("dev", 1)
    poller = Poller(device, 2, 10000, 8, "BE_BE")
    ref = Reference(device, "di01", "10000", "bool", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"di01": ref}

    master = FakeModbusMaster(coils=[False] * 8)
    handler = handler_with_device(device, master)
    assert handler.write_reference("dev", "di01", True) is False
    assert master.writes == []


def test_export_json_shape(tmp_path):
    device = Device("modsim01", 1)
    ref = Reference(device, "holding_reg01", "40000", "uint16", "r", None, None)
    ref.val = 100
    device.references = {"holding_reg01": ref}

    handler = ModbusHandler(MagicMock(), "dummy.csv")
    handler.deviceList = [device]
    export_file = tmp_path / "out.json"
    handler.export(str(export_file))

    data = json.loads(export_file.read_text())
    assert data == {"modsim01": {"holding_reg01": 100}}


def test_export_includes_timestamp_when_provided(tmp_path):
    device = Device("dev", 1)
    ref = Reference(device, "temp", "0", "uint16", "r", None, None)
    ref.val = 21
    device.references = {"temp": ref}

    handler = ModbusHandler(MagicMock(), "dummy.csv")
    handler.deviceList = [device]
    export_file = tmp_path / "out.json"
    handler.export(str(export_file), timestamp=1710000000.0)

    data = json.loads(export_file.read_text())
    assert data["dev"]["timestamp"] == 1710000000.0
    assert data["dev"]["temp"] == 21


# ---------------------------------------------------------------------------
# Config parsing contracts
# ---------------------------------------------------------------------------


def test_parse_config_accepts_hex_address():
    config = "\n".join(
        [
            "device,dev,1",
            "poll,holding_register,16,2,BE_BE",
            "ref,hex_ref,0x10,uint16,r",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 1
    assert devices[0].references["hex_ref"].address == 0x10


def test_poller_rejects_register_poll_over_123(caplog):
    config = "\n".join(
        [
            "device,dev,1",
            "poll,holding_register,0,124,BE_BE",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("ERROR"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 1
    assert devices[0].pollerList == []
    assert any("Too many registers" in m for m in log_messages(caplog, "ERROR"))


def test_poller_rejects_coil_poll_over_2000(caplog):
    config = "\n".join(
        [
            "device,dev,1",
            "poll,coil,0,2001,BE_BE",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("ERROR"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 1
    assert devices[0].pollerList == []
    assert any("Too many coils" in m for m in log_messages(caplog, "ERROR"))


def test_warn_writable_mark_on_discrete_input(caplog):
    config = "\n".join(
        [
            "device,dev,1",
            "poll,discrete_input,10000,8,BE_BE",
            "ref,di01,10000,bool,rw",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("WARNING"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 1
    assert "di01" in devices[0].references
    warnings = log_messages(caplog, "WARNING")
    assert any(
        "di01" in m and "discrete_input" in m and "read-only" in m for m in warnings
    )


def test_warn_writable_mark_on_input_register(caplog):
    config = "\n".join(
        [
            "device,dev,1",
            "poll,input_register,30000,2,BE_BE",
            "ref,input_reg01,30000,uint16,w",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("WARNING"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 1
    assert "input_reg01" in devices[0].references
    warnings = log_messages(caplog, "WARNING")
    assert any(
        "input_reg01" in m and "input_register" in m and "read-only" in m
        for m in warnings
    )


def test_no_warn_read_only_mark_on_discrete_input(caplog):
    config = "\n".join(
        [
            "device,dev,1",
            "poll,discrete_input,10000,8,BE_BE",
            "ref,di01,10000,bool,r",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("WARNING"):
        handler._parse_config(csv.reader(io.StringIO(config)))
    assert not any("read-only" in m for m in log_messages(caplog, "WARNING"))


def test_invalid_endian_aborts_config_load(caplog):
    config = "\n".join(
        [
            "device,dev,1",
            "poll,holding_register,0,1,NOT_A_VALID_ENDIAN",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("ERROR"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert devices == []
    assert "Invalid endian" in caplog.text


def test_duplicate_device_name_aborts_config(caplog):
    config = "\n".join(
        [
            "device,same,1",
            "poll,holding_register,0,1,BE_BE",
            "device,same,2",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("ERROR"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert devices == []
    assert "Duplicate device name 'same'" in caplog.text


def test_shared_slave_id_logs_warning_and_loads(caplog):
    config = "\n".join(
        [
            "device,devA,1",
            "poll,holding_register,0,1,BE_BE",
            "device,devB,1",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("WARNING"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 2
    assert {d.name for d in devices} == {"devA", "devB"}
    assert devices[0].devid == devices[1].devid == 1
    assert "Modbus slave ID 1 shared by logical devices: devA, devB" in caplog.text


def test_shared_slave_id_adjacent_ranges_no_overlap_warning(caplog):
    config = "\n".join(
        [
            "device,cta_conf,1",
            "poll,coil,0,14,BE_BE",
            "ref,BP_MA_CTA,0,bool,rw",
            "poll,holding_register,0,7,BE_BE",
            "ref,PID_EC,0,int16,rw",
            "device,cta_rest,1",
            "poll,coil,14,8,BE_BE",
            "ref,BP_MA_CTA,14,bool,rw",
            "poll,holding_register,7,6,BE_BE",
            "ref,PID_EC,7,int16,rw",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("WARNING"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 2
    assert (
        "Modbus slave ID 1 shared by logical devices: cta_conf, cta_rest" in caplog.text
    )
    assert "Overlapping Modbus poll ranges" not in caplog.text


def test_overlapping_poller_ranges_warns(caplog):
    config = "\n".join(
        [
            "device,devA,1",
            "poll,coil,0,10,BE_BE",
            "ref,a,0,bool,r",
            "device,devB,1",
            "poll,coil,5,10,BE_BE",
            "ref,b,5,bool,r",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("WARNING"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 2
    assert "Overlapping Modbus poll ranges on slave ID 1 (fc=1)" in caplog.text
    assert "device 'devA' [0, 10)" in caplog.text
    assert "device 'devB' [5, 15)" in caplog.text
    assert "shared by logical devices" not in caplog.text


def test_duplicate_poller_skipped(caplog):
    config = "\n".join(
        [
            "device,dev,1",
            "poll,holding_register,0,10,BE_BE",
            "ref,first,0,uint16,r",
            "poll,holding_register,0,10,BE_BE",
            "ref,second,1,uint16,r",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("WARNING"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 1
    assert len(devices[0].pollerList) == 1
    ref_names = {r.name for r in devices[0].pollerList[0].readableReferences}
    assert ref_names == {"first", "second"}
    assert "Duplicate poller on device dev" in caplog.text


def test_duplicate_ref_across_pollers_ignored(caplog):
    config = "\n".join(
        [
            "device,dev,1",
            "poll,holding_register,40000,10,BE_BE",
            "ref,temp,40001,uint16,r",
            "poll,holding_register,40010,5,BE_BE",
            "ref,copy,40001,uint16,r",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("WARNING"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 1
    assert len(devices[0].pollerList) == 2
    assert "temp" in devices[0].references
    assert "copy" not in devices[0].references
    assert "duplicates address/dtype in another poller" in caplog.text


def test_poller_size_zero_ignored(caplog):
    config = "\n".join(
        [
            "device,dev,1",
            "poll,holding_register,0,0,BE_BE",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("ERROR"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 1
    assert len(devices[0].pollerList) == 0
    assert "Poller size must be greater than 0" in caplog.text
