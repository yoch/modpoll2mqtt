import sys
from unittest.mock import MagicMock

import pytest

from modpoll import main
from modpoll.arg_parser import get_parser


def test_mqtt_tls_cli_options_forwarded(monkeypatch):
    captured = {}

    class FakeMqttHandler:
        def __init__(
            self,
            name,
            host,
            port,
            user,
            password,
            clientid,
            qos,
            subscribe_topics,
            use_tls,
            tls_version,
            cacerts,
            insecure,
            mqtt_version,
            log_level,
            rx_queue_size=1000,
            retain_data_publishes=False,
        ):
            captured["init"] = {
                "name": name,
                "host": host,
                "port": port,
                "user": user,
                "password": password,
                "clientid": clientid,
                "qos": qos,
                "subscribe_topics": subscribe_topics,
                "use_tls": use_tls,
                "tls_version": tls_version,
                "cacerts": cacerts,
                "insecure": insecure,
                "mqtt_version": mqtt_version,
                "log_level": log_level,
                "rx_queue_size": rx_queue_size,
                "retain_data_publishes": retain_data_publishes,
            }

        def setup(self):
            return True

        def connect(self):
            return True

        def close(self):
            captured["closed"] = True

        def receive(self):
            return None, None

    def fake_setup_modbus_handlers(args, mqtt_handler):
        captured["handler_instance"] = mqtt_handler
        # Stop execution before entering the main polling loop
        raise SystemExit(0)

    monkeypatch.setattr(main, "MqttHandler", FakeMqttHandler)
    monkeypatch.setattr(main, "setup_modbus_handlers", fake_setup_modbus_handlers)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modpoll",
            "--config",
            "dummy.csv",
            "--tcp",
            "127.0.0.1",
            "--mqtt-host",
            "broker.local",
            "--mqtt-use-tls",
            "--mqtt-cacerts",
            "/tmp/ca.pem",
            "--mqtt-tls-version",
            "tlsv1.2",
            "--mqtt-version",
            "3.1.1",
            "--mqtt-insecure",
            "--mqtt-rx-queue-size",
            "500",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        main.app()

    assert excinfo.value.code == 0
    init_args = captured["init"]
    assert init_args["rx_queue_size"] == 500
    assert init_args["use_tls"] is True
    assert init_args["tls_version"] == "tlsv1.2"
    assert init_args["cacerts"] == "/tmp/ca.pem"
    assert init_args["insecure"] is True
    assert init_args["mqtt_version"] == "3.1.1"
    assert init_args["subscribe_topics"] == ["modpoll/+/set", "modpoll/+/get"]
    assert init_args["retain_data_publishes"] is False


def test_mqtt_retain_cli_option_forwarded(monkeypatch):
    captured = {}

    class FakeMqttHandler:
        def __init__(self, *args, retain_data_publishes=False, **kwargs):
            captured["retain_data_publishes"] = retain_data_publishes

        def setup(self):
            return True

        def connect(self):
            return True

        def close(self):
            pass

        def receive(self):
            return None, None

    def fake_setup_modbus_handlers(args, mqtt_handler):
        raise SystemExit(0)

    monkeypatch.setattr(main, "MqttHandler", FakeMqttHandler)
    monkeypatch.setattr(main, "setup_modbus_handlers", fake_setup_modbus_handlers)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modpoll",
            "--config",
            "dummy.csv",
            "--tcp",
            "127.0.0.1",
            "--mqtt-host",
            "broker.local",
            "--mqtt-retain",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        main.app()

    assert excinfo.value.code == 0
    assert captured["retain_data_publishes"] is True


def test_csv_delimiter_invalid_code_rejected():
    parser = get_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--config",
                "dummy.csv",
                "--tcp",
                "127.0.0.1",
                "--csv-delimiter",
                "pipe",
            ]
        )


def test_mqtt_rx_queue_size_zero_exits(monkeypatch, caplog):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modpoll",
            "--config",
            "dummy.csv",
            "--tcp",
            "127.0.0.1",
            "--mqtt-host",
            "broker.local",
            "--mqtt-rx-queue-size",
            "0",
        ],
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(SystemExit) as excinfo:
            main.app()

    assert excinfo.value.code == 1
    assert "MQTT rx queue size must be at least 1" in caplog.text


def test_mqtt_subscribe_pattern_without_plus_exits(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modpoll",
            "--config",
            "dummy.csv",
            "--tcp",
            "127.0.0.1",
            "--mqtt-host",
            "broker.local",
            "--mqtt-subscribe-topic-pattern",
            "modpoll/set",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        main.app()

    assert excinfo.value.code == 1


def test_extract_device_from_mqtt_topic():
    pattern = "modpoll/+/set"
    assert (
        main.extract_device_from_mqtt_topic(pattern, "modpoll/cta_conf/set")
        == "cta_conf"
    )
    assert main.extract_device_from_mqtt_topic(pattern, "modpoll/dev/set") == "dev"
    assert main.extract_device_from_mqtt_topic(pattern, "xmodpoll/dev/set") is None


def test_classify_mqtt_command_topic():
    set_p = "modpoll/+/set"
    get_p = "modpoll/+/get"
    assert main.classify_mqtt_command_topic(set_p, get_p, "modpoll/dev/set") == (
        "set",
        "dev",
    )
    assert main.classify_mqtt_command_topic(set_p, get_p, "modpoll/dev/get") == (
        "get",
        "dev",
    )
    assert main.classify_mqtt_command_topic(set_p, get_p, "other/dev/set") == (
        None,
        None,
    )


def test_mqtt_get_response_topic():
    assert (
        main.mqtt_get_response_topic("modpoll/+/get", "cta_conf")
        == "modpoll/cta_conf/get/response"
    )


def test_mqtt_setup_close_errors_are_suppressed(monkeypatch):
    class FakeMqttHandler:
        def __init__(self, *args, **kwargs):
            pass

        def setup(self):
            raise RuntimeError("setup failed")

        def connect(self):
            return False

        def close(self):
            raise RuntimeError("close exploded")

    monkeypatch.setattr(main, "MqttHandler", FakeMqttHandler)
    monkeypatch.setattr(
        main, "setup_modbus_handlers", lambda args, mqtt_handler: (MagicMock(), [])
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modpoll",
            "--config",
            "dummy.csv",
            "--tcp",
            "127.0.0.1",
            "--mqtt-host",
            "broker.local",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        main.app()

    assert excinfo.value.code == 1


def test_interval_default_depends_on_transport():
    parser = get_parser()

    tcp_args = parser.parse_args(["--config", "dummy.csv", "--tcp", "127.0.0.1"])
    udp_args = parser.parse_args(["--config", "dummy.csv", "--udp", "127.0.0.1"])
    serial_args = parser.parse_args(
        ["--config", "dummy.csv", "--serial", "/dev/ttyUSB0"]
    )
    slow_serial_args = parser.parse_args(
        [
            "--config",
            "dummy.csv",
            "--serial",
            "/dev/ttyUSB0",
            "--serial-baud",
            "1200",
        ]
    )

    assert tcp_args.interval == 0.0
    assert udp_args.interval == 0.0
    assert serial_args.interval == 0.005
    assert slow_serial_args.interval == pytest.approx(3.5 * 11 / 1200)


def test_explicit_interval_overrides_transport_default():
    args = get_parser().parse_args(
        [
            "--config",
            "dummy.csv",
            "--tcp",
            "127.0.0.1",
            "--interval",
            "0.25",
        ]
    )

    assert args.interval == 0.25


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--interval", "-0.1"],
        ["--serial", "/dev/ttyUSB0", "--serial-baud", "0"],
        ["--modbus-backoff-base", "-1"],
        ["--modbus-backoff-base", "2", "--modbus-backoff-max", "1"],
        ["--modbus-max-connection-age", "0"],
    ],
)
def test_invalid_timing_options_are_rejected(extra_args):
    parser = get_parser()
    argv = ["--config", "dummy.csv", "--tcp", "127.0.0.1", *extra_args]

    with pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_parse_known_args_applies_interval_default_and_validation():
    parser = get_parser()
    args, extras = parser.parse_known_args(
        ["--config", "dummy.csv", "--tcp", "127.0.0.1", "--unknown", "x"]
    )

    assert args.interval == 0.0
    assert extras == ["--unknown", "x"]

    with pytest.raises(SystemExit):
        parser.parse_known_args(
            [
                "--config",
                "dummy.csv",
                "--tcp",
                "127.0.0.1",
                "--interval",
                "-1",
            ]
        )
