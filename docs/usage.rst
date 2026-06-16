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
- ``--interval`` waits between pollers and between successive references in a single MQTT write command. If omitted, the default is transport-aware: ``0.0`` seconds for TCP/UDP; for serial/RTU it is derived from ``--serial-baud`` using the Modbus RTU 3.5-character silent interval with a practical ``0.005`` s floor. Set it explicitly for slow devices that need extra settling time.

Default serial/RTU examples:

.. list-table::
   :header-rows: 1

   * - ``--serial-baud``
     - Auto ``--interval``
   * - ``1200``
     - ``0.03208`` s
   * - ``2400``
     - ``0.01604`` s
   * - ``4800``
     - ``0.00802`` s
   * - ``9600``
     - ``0.005`` s
   * - ``19200`` and above
     - ``0.005`` s

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

Persistent Modbus connection
----------------------------

``modpoll`` keeps the Modbus client open between poll cycles and MQTT commands. The connection is closed on process shutdown and after transport failures, then retried with a non-blocking exponential backoff. This avoids repeated connect/close overhead while preventing a dead bus or half-open socket from blocking the main loop for a long backoff sleep.

Operational notes:

- ``--timeout`` still bounds individual Modbus operations at the pymodbus client level.
- ``--interval`` defaults to ``0.0`` on TCP/UDP so persistent connections are not hidden behind an artificial 0.5 s poller delay. Serial/RTU derives its default from ``--serial-baud`` using the Modbus RTU 3.5-character silent interval, with a ``0.005`` s floor for practical scheduling. The same delay is used between pollers in a poll cycle and between references in one MQTT ``set`` message.
- ``--modbus-backoff-base`` and ``--modbus-backoff-max`` control reconnect pacing after failures.
- ``--modbus-max-connection-age`` can recycle a long-lived connection periodically; it is disabled by default.
- On serial/RTU transports, the port remains reserved while ``modpoll`` runs. Stop ``modpoll`` before debugging the same port with another tool.
- Transport exceptions raised during polling, MQTT get, or MQTT set close the Modbus connection and enter backoff immediately. Modbus exception responses returned by a device are counted as operation failures without forcing a reconnect.


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
    "get_count": 5,
    "get_errors": 2,
    "get_success": 3,
    "get_unknown_refs": 1,
    "get_read_errors": 4,
    "set_count": 8,
    "set_errors": 1,
    "set_success": 7,
    "set_unknown_refs": 2,
    "config_source": "/path/to/config.csv"
  }

**Process-wide** (``modpoll/diagnostics``):

.. code-block:: json

  {
    "mqtt_connected": true,
    "modbus_ok": true,
    "devices_failing": 1,
    "last_cycle_s": 10.1,
    "modbus_connection_state": "READY",
    "modbus_connected": true,
    "modbus_connected_since": 1760000000.0,
    "modbus_last_success_at": 1760000010.0,
    "modbus_last_failure_at": null,
    "modbus_last_error": null,
    "modbus_consecutive_failures": 0,
    "modbus_backoff_until": null,
    "modbus_connect_count": 1,
    "modbus_reconnect_count": 0,
    "modbus_transaction_failure_count": 0
  }

Diagnostics retain follows ``--mqtt-retain`` (same as data topics). Diagnostics report operational health; they are not a substitute for ``modpoll/status`` presence. ``last_cycle_s`` is ``0`` until the first poll cycle completes. ``modbus_ok`` reports whether the Modbus transport was available for all scheduled poll handlers in the latest cycle; per-device logical failures are reflected separately by ``devices_failing`` and device diagnostics. When Modbus is unavailable, ``modbus_connection_state``, ``modbus_last_error`` and ``modbus_backoff_until`` show whether the process is waiting before the next reconnect attempt.

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
- Unknown reference keys are skipped with a warning; ``set_unknown_refs``, ``set_errors``, and ``set_success`` in device diagnostics track write attempts (see MQTT diagnostics).

Duplicate reference names on the same device are rejected when loading the config file.

MQTT on-demand read (get)
-------------------------

Subscribe pattern (default): ``modpoll/+/get``. Publish to ``modpoll/{device}/get`` with a JSON object whose keys are reference names (values are ignored, use ``null``):

.. code-block:: json

  {
    "temp": null,
    "pressure": null
  }

- On success, the response is published to ``modpoll/{device}/get/response`` with a JSON object of reference names to decoded values (same units as periodic MQTT publish). References that could not be read are omitted from the payload.
- An empty payload ``{}`` on the request is ignored (warning logged, no response, no diagnostics). An empty response ``{}`` means the request was processed but no value could be read.
- On partial failure, ``get_errors`` is incremented once per request (and ``get_success`` when there were no errors). ``get_unknown_refs`` and ``get_read_errors`` count individual skipped or failed references.
- Multiple references in one request are read independently: known refs are returned even if others fail.
- Reads use targeted Modbus requests (minimal register/coil count per reference), not a full poller block.
- Uses the shared persistent Modbus connection; if the connection is in backoff, the request fails quickly and returns an empty response.

**Breaking change (2.1.0+):** the ``ref``/``value`` object format is no longer supported; use a reference map instead.

**Breaking change (2.0.0+):** the previous low-level format (``object_type``, ``address``, ``value``) is no longer supported.
