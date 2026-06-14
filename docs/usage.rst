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

By default, published data messages are **not** retained by the broker. Use ``--mqtt-retain`` to set the MQTT retain flag on data publishes (``publish_data`` only; diagnostics are never retained).

This is useful when subscribers (dashboards, automations) connect after ``modpoll`` has already started: they receive the last known values immediately instead of waiting for the next poll cycle.

.. code-block:: shell

    modpoll --tcp 192.168.1.10 --mqtt-host localhost --mqtt-retain --config examples/modsim.csv

**Caveats:**

- If a Modbus device becomes unreachable, ``modpoll`` stops publishing for that device but the broker may still serve the last retained message, which can look like a live value.
- Retain is not a last-will/offline signal; it only stores the last successful publish per topic.

MQTT payload keys
-----------------

By default, grouped MQTT publish payloads use reference names as JSON keys, appending ``\|unit`` when a unit is configured in the CSV (e.g. ``"temp\|°C"``). Use ``--mqtt-keys name-only`` to publish keys without the unit suffix:

.. code-block:: shell

    modpoll --tcp 192.168.1.10 --mqtt-host localhost --mqtt-keys name-only --config examples/modsim.csv

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
- Multiple references can be written in a single message.

Duplicate reference names on the same device are rejected when loading the config file.

**Breaking change (2.1.0+):** the ``ref``/``value`` object format is no longer supported; use a reference map instead.

**Breaking change (2.0.0+):** the previous low-level format (``object_type``, ``address``, ``value``) is no longer supported.
