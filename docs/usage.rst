Basic Usage
===========

.. argparse::
    :ref: modpoll.arg_parser.get_parser
    :prog: modpoll

The `config` option is required.


Commandline Usage
------------------

- Connect to Modbus TCP device

  .. code-block:: shell

    modpoll --tcp 192.168.1.10 --config examples/modsim.csv

- Connect to Modbus serial device

  .. code-block:: shell

    modpoll --serial /dev/ttyUSB0 --serial-baud 9600 --config contrib/eniwise/scpms6.csv

- Connect to Modbus TCP device and publish data to remote MQTT broker

  .. code-block:: shell

    modpoll --tcp 192.168.1.10 --config examples/modsim.csv --mqtt-host broker.emqx.io

- Connect to Modbus TCP device and export data to local csv file

  .. code-block:: shell

    modpoll --tcp 192.168.1.10 --config examples/modsim.csv --export data.csv

- Connect to Modbus UDP device

  .. code-block:: shell

    modpoll --udp 192.168.1.10 --config examples/modsim.csv

Configuration sources
---------------------

``--config`` accepts one or more local file paths or HTTP(S) URLs. Multiple files are loaded into separate logical configs that share the same Modbus connection.

If columns are not split correctly (for example tab-separated files), use ``--csv-delimiter tab`` (default: ``comma``).

Export
------

``--export`` writes polled reference values to a JSON file keyed by device name, then by reference name. Use ``--timestamp`` to add a ``timestamp`` field to each device's export object and to grouped MQTT publish payloads.

.. code-block:: shell

    modpoll --tcp 192.168.1.10 --config examples/modsim.csv --export data.json --timestamp

Operational flags
-----------------

- ``--no-output`` suppresses poll result tables on stdout (replaces the former ``--daemon`` / ``-d`` flag; does not fork).
- ``--delay`` waits N seconds after connecting before the first Modbus poll.

Configuration File
------------------

The configuration file (`--config`) is a CSV file that defines the devices, pollers, and references to be read.

Coil and discrete input references
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

On ``coil`` or ``discrete_input`` pollers:

- ``5,bool`` reads a **single coil/discrete input** at Modbus address 5 and publishes one boolean value.
- ``0,bool8`` / ``0,bool16`` read a **legacy bit group** (8 or 16 booleans). With ``poll,coil,0,16``, group address ``1`` returns Modbus coil addresses 8–15 (often labeled coils 9–16 in vendor tables). If the poll ends before the full group is read, missing bits are padded with ``false``.
- ``address:bit`` syntax is **not** supported on coil/discrete_input pollers.

Register bit references
^^^^^^^^^^^^^^^^^^^^^^^

For register references (i.e., Holding or Input registers) with a ``dtype`` of ``bool``, you can specify a single bit to be extracted from the 16-bit register. This is done by appending ``:bit`` to the address, where ``bit`` is an integer from 0 to 15.

- ``40110``: Reads the entire 16-bit register at address 40110.
- ``40110:15``: Reads the 16-bit register at address 40110, extracts bit 15, and returns a boolean value.

The bit is extracted from the final 16-bit value after byte/word swapping based on the poller's endianness configuration.

Framers and transports
----------------------

- Serial (`--serial`, alias `--rtu`) supports framers `rtu` and `ascii` (e.g., `--serial ... --framer ascii`). Binary framer was removed in pymodbus 3.9+. If `--framer default` is used, pymodbus defaults to RTU framer.
- TCP/UDP (`--tcp`/`--udp`) use the `socket` framer; other framers are rejected. If `--framer default` is used, pymodbus defaults to socket framer.

MQTT retain
-----------

By default, published data and diagnostics messages are **not** retained by the broker. Use ``--mqtt-retain`` to set the MQTT retain flag on data and diagnostics publishes (``publish_data`` and ``--diagnostics-rate`` topics). The status topic ``modpoll/status`` is always retained independently of ``--mqtt-retain``.

This is useful when subscribers (dashboards, automations) connect after ``modpoll`` has already started: they receive the last known values immediately instead of waiting for the next poll cycle.

.. code-block:: shell

    modpoll --tcp 192.168.1.10 --mqtt-host localhost --mqtt-retain --config examples/modsim.csv

**Caveats:**

- If a Modbus device becomes unreachable, ``modpoll`` stops publishing for that device but the broker may still serve the last retained message, which can look like a live value.
- Retain is not a last-will/offline signal for data or diagnostics; use ``modpoll/status`` for process presence (see below).

MQTT status
-----------

When ``--mqtt-host`` is set, ``modpoll`` publishes process presence on ``modpoll/status``:

.. code-block:: json

  {"online": true}

On a clean shutdown, ``online`` is set to ``false`` before disconnecting. On an unexpected disconnect (crash, network loss), the broker publishes a last-will message with ``{"online": false}``. Status messages are **always** retained.

MQTT diagnostics
----------------

When ``--diagnostics-rate`` is greater than zero, ``modpoll`` periodically publishes diagnostics.

**Per device** (``--mqtt-diagnostics-topic-pattern``, default ``modpoll/{device}/diagnostics``):

.. code-block:: json

  {
    "poll_count": 42,
    "error_count": 3,
    "last_poll_success": true,
    "config_source": "/path/to/config.csv"
  }

**Process-wide** (``modpoll/diagnostics``):

.. code-block:: json

  {
    "mqtt_connected": true,
    "modbus_ok": true,
    "devices_failing": 1,
    "last_cycle_s": 10.1
  }

Diagnostics retain follows ``--mqtt-retain`` (same as data topics). Diagnostics report operational health; they are not a substitute for ``modpoll/status`` presence. ``last_cycle_s`` is ``0`` until the first poll cycle completes.

MQTT payload keys
-----------------

By default, grouped MQTT publish payloads use reference names as JSON keys, appending ``\|unit`` when a unit is configured in the CSV (e.g. ``"temp\|°C"``). Use ``--mqtt-keys name-only`` to publish keys without the unit suffix:

.. code-block:: shell

    modpoll --tcp 192.168.1.10 --mqtt-host localhost --mqtt-keys name-only --config examples/modsim.csv

MQTT single publish
-------------------

By default, all references for a device are published in one JSON object on the data topic. With ``--mqtt-single``, each reference is published on its own topic under the publish pattern, e.g. ``modpoll/{device}/data/{ref_name}``. List values (``bool8`` / ``bool16``) are split into indexed sub-topics (``.../{ref_name}/0``, ``.../1``, …).

Publish behavior
----------------

- Data is not published for a device unless its latest poll cycle succeeded (``last_poll_success`` in diagnostics).
- References with no value (``null``) are omitted from grouped MQTT payloads.
- Non-finite floats (``NaN``, ``Inf``) are omitted from MQTT publish and export.

MQTT write commands
-------------------

Subscribe pattern (default): ``modpoll/+/set``. Publish to ``modpoll/{device}/set`` with a JSON object mapping reference names to values:

.. code-block:: json

  {
    "PID_V3V_EC_Consigne_reprise": 21.5,
    "BP_MA_CTA": true
  }

- The **device** is taken from the MQTT topic, not from the JSON payload.
- Reference names in the payload must match the CSV configuration; unknown keys are skipped with a warning.
- Values use the same decoded engineering units as MQTT publish (scale and dtype from the CSV are handled by modpoll).
- Only references marked ``rw`` or ``w`` in the CSV can be written.
- Only ``coil`` and ``holding_register`` pollers are writable; ``discrete_input`` and ``input_register`` are read-only at the Modbus protocol level even when marked ``rw``.
- ``bool`` on coils or registers (including ``address:bit``) expects a scalar boolean.
- ``bool8`` / ``bool16`` expect a JSON array of 8 or 16 booleans.
- ``stringNNN`` references expect a string value.
- Multiple references can be written in a single message.

Duplicate reference names on the same device are rejected when loading the config file.

**Breaking change (2.1.0+):** the ``ref``/``value`` object format is no longer supported; use a reference map instead.

**Breaking change (2.0.0+):** the previous low-level format (``object_type``, ``address``, ``value``) is no longer supported.
