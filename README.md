# modpoll2mqtt — Modbus to MQTT Gateway

[![Release](https://img.shields.io/github/v/release/yoch/modpoll2mqtt?label=release)](https://github.com/yoch/modpoll2mqtt/releases/latest)
[![License](https://img.shields.io/pypi/l/modpoll2mqtt)](https://github.com/yoch/modpoll2mqtt/blob/main/LICENSE)
[![checks](https://img.shields.io/github/actions/workflow/status/yoch/modpoll2mqtt/ci-check.yml?branch=main&label=checks)](https://github.com/yoch/modpoll2mqtt/actions/workflows/ci-check.yml?query=branch%3Amain)
[![tests](https://img.shields.io/github/actions/workflow/status/yoch/modpoll2mqtt/ci-test.yml?branch=main&label=tests)](https://github.com/yoch/modpoll2mqtt/actions/workflows/ci-test.yml?query=branch%3Amain)
[![Downloads](https://img.shields.io/pepy/dt/modpoll2mqtt)](https://pepy.tech/projects/modpoll2mqtt)

> Documentation: [yoch.github.io/modpoll2mqtt](https://yoch.github.io/modpoll2mqtt)

**modpoll2mqtt** is a command-line Modbus-to-MQTT gateway. It polls Modbus devices (RTU, TCP, UDP) using CSV configuration files, publishes values to an MQTT broker, and accepts write commands by CSV reference name.

Install the PyPI package as `modpoll2mqtt`; the executable command remains `modpoll`.

Fork of [modpoll](https://github.com/gavinying/modpoll), using the latest pymodbus release, with semantic MQTT writes by CSV reference name and numerous bugfixes and improvements.

## Features

- Modbus RTU, TCP, and UDP (CSV configuration files)
- Local display of polled data (debug mode)
- MQTT publishing of references (configurable topics)
- MQTT writes by CSV reference name (`modpoll/<device>/set`)
- Local CSV export of polled data
- Bit-level access on registers and coils

## Installation

Python 3.10+ required.

```bash
pip install modpoll2mqtt
```

Optional serial support (pyserial):

```bash
pip install 'modpoll2mqtt[serial]'
```

Upgrade:

```bash
pip install -U modpoll2mqtt
```

On Windows, [pipx](https://pypa.github.io/pipx/installation/) is recommended:

```powershell
pipx install modpoll2mqtt
```

## Quickstart

Single poll of a Modbus TCP device (replace the IP address with yours):

```bash
modpoll --once \
  --tcp 192.168.1.10 \
  --config examples/modsim.csv
```

Publish to an MQTT broker:

```bash
modpoll \
  --tcp 192.168.1.10 \
  --mqtt-host broker.emqx.io \
  --config examples/modsim.csv
```

Data is published to `modpoll/<device_name>/data` by default.

Add `--mqtt-retain` so the broker keeps the last data and diagnostics message per topic for new subscribers. The status topic `modpoll/status` is always retained. If a device goes offline, retained data values may still appear live until the next successful publish.

### MQTT write by reference

Once connected to the broker, `modpoll` subscribes to `modpoll/+/set`. Publish to `modpoll/<device>/set`:

```json
{
  "holding_reg01": 100,
  "coil01-08": [true, false, false, false, false, false, false, false]
}
```

Unknown reference names in the payload are skipped with a warning.

`bool8` / `bool16` references require a JSON array of 8 or 16 booleans at write time. Single-coil `bool` references accept a scalar boolean.

Example: an `int16` reference with scale `0.1` accepts `21.5` as input; the raw register value `215` is written.

Only `coil` and `holding_register` references can be written. `input_register` and `discrete_input` are read-only at the Modbus protocol level even when marked `rw` in the CSV.

**Migration from modpoll 1.6.x:** the low-level format (`object_type`, `address`, `value`) is no longer supported.

**Migration from modpoll2mqtt 2.0.x:** the `{"ref": "...", "value": ...}` object format was removed in 2.1.0; use a reference map (`{"ref_name": value}`) instead.

### Configuration pitfalls

- On **coil** or **discrete_input** pollers, use `bool` with the **absolute Modbus coil address** for a single boolean.
- `bool8` / `bool16`: legacy grouped reads (group index, not a direct coil address).
- On **holding_register** or **input_register**, use `bool` with `address:bit` (0–15), e.g. `40019:15,bool`.
- `<endian>` must be `BE_BE`, `LE_BE`, `LE_LE`, or `BE_LE` only.
- `--autoremove` disables a poller after 3 consecutive Modbus failures.

## Examples

```bash
# Modbus TCP
modpoll --tcp 192.168.1.10 --config examples/modsim.csv

# Modbus serial
modpoll --serial /dev/ttyUSB0 --serial-baud 9600 --config contrib/eniwise/scpms6.csv

# MQTT + CSV export
modpoll --tcp 192.168.1.10 --mqtt-host localhost --export data.csv --config examples/modsim.csv

# Multiple config files
modpoll --tcp 192.168.1.10 --config examples/modsim.csv examples/modsim2.csv
```

See [`examples/`](examples/) and [`contrib/`](contrib/) for more device configurations.

## Credits

This project builds on:

- [modpoll](https://github.com/gavinying/modpoll) — Ying Shaodong (MIT)
- [modbus2mqtt](https://github.com/owagner/modbus2mqtt) — Oliver Wagner (MIT)
- [spicierModbus2mqtt](https://github.com/mbs38/spicierModbus2mqtt) — Max Brueggemann (MIT)

## License

MIT — see [LICENSE](LICENSE).
