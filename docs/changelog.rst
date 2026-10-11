Changelog
=========

`2.3.1 <https://github.com/yoch/modpoll2mqtt/compare/v2.3.0...v2.3.1>`__ (2026-10-11)
-------------------------------------------------------------------------------------

Fixes
~~~~~

-  make pyserial optional again now that pymodbus 3.16.1 imports without
   it
   (`pymodbus#3042 <https://github.com/pymodbus-dev/pymodbus/issues/3042>`__);
   pymodbus 3.16.0 is excluded. Serial/RTU users install
   ``modpoll2mqtt[serial]``, as before 2.3.0

.. _section-1:

`2.3.0 <https://github.com/yoch/modpoll2mqtt/compare/v2.2.1...v2.3.0>`__ (2026-10-11)
-------------------------------------------------------------------------------------

Features
~~~~~~~~

-  add ``--check-config`` for whole-configuration validation without
   opening Modbus/MQTT transports
-  add ``--read-only`` to reject all MQTT writes and
   ``--modbus-bit-write mask`` for optional atomic FC22 register updates
-  add ``--poller-recover-after`` for optional re-probing after
   ``--autoremove`` disables a poller
-  add ``--mqtt-namespace`` and ``--mqtt-command-topic-pattern`` for
   isolated gateway topics and versioned get/set commands with
   per-reference results and bounded process-local request deduplication
-  add ``--mqtt-max-payload-bytes``, ``--mqtt-queue-bytes``,
   ``--mqtt-tx-queue-size``, and ``--mqtt-command-ttl`` to bound command
   resources and age
-  add ``--mqtt-shutdown-timeout``, ``--mqtt-session-expiry``, and
   ``--mqtt-pass-file`` for publication flushing, explicit persistent
   sessions, and file-based credentials
-  expose per-reference quality/acquisition times and MQTT
   rejection/publication/subscription counters in diagnostics

.. _fixes-1:

Fixes
~~~~~

-  bind references to their declared function code/endian instead of the
   first overlapping range; retain distinct aliases when identical
   pollers are reused
-  reject unsafe numeric writes before bus I/O; make register bit writes
   inverse to decoding; target only the declared coil group
-  clear failed/short/non-finite decodes without retaining a stale
   current value; isolate a malformed UTF-8 string from other references
-  saturate reconnect backoff, schedule with monotonic time, avoid
   counting unattempted polls, disable pymodbus write retries, and
   guarantee transport cleanup
-  ignore retained commands (MQTT 5 preserves retain on live delivery),
   reject malformed/duplicate-key JSON, bound incoming/outgoing queues,
   reconnect MQTT asynchronously, and republish retained presence after
   reconnect
-  aggregate all configurations into one atomic JSON export and report
   partial writes/uncertain acknowledgements without blind replay
-  repair shipped SDM120M permissions, SmartLogger dtype, and TriStar
   duplicate names (second occurrences use their address suffix)
-  require pyserial so the package still imports with pymodbus 3.16.0,
   which fails without it
   (`pymodbus#3042 <https://github.com/pymodbus-dev/pymodbus/issues/3042>`__);
   the ``serial`` extra is kept for compatibility

BREAKING CHANGES
~~~~~~~~~~~~~~~~

-  invalid CSV entries previously skipped now abort startup (exit 2);
   device names must be unique across files, scales finite/nonzero,
   IDs/ranges/dtypes/permissions valid
-  unsigned overflow no longer wraps; out-of-range, non-finite,
   fractional unscaled integer writes and oversized UTF-8 strings are
   rejected; scaled integer writes round the scaled raw value
-  TLS 1.0/1.1 are rejected; sessions are ephemeral by default at every
   QoS; explicit persistent sessions require ``--mqtt-clientid``
-  ``--once`` runs polling only and returns 1 on poll/runtime/MQTT
   publication failure; command responses are never retained, even with
   ``--mqtt-retain``
-  legacy SET now returns per-reference results on
   ``<request-topic>/response``; all known writes are validated before
   any are attempted, and conflicting ranges in a single command are
   rejected
-  shipped ``contrib/`` configurations changed: TriStar duplicate names
   are renamed (their MQTT topics and payload keys change), SDM120M
   permissions and the SmartLogger dtype are corrected

Tests and CI
~~~~~~~~~~~~

-  add regression coverage for the audit defects, all supported numeric
   endians, strict commands, batch prevalidation, lost acknowledgements,
   queue budgets, recovery, atomic export, and exit codes
-  run real CLI/Modbus/MQTT 5 and 3.1.1 exchanges with local Mosquitto
   and independent slave/object spaces; required CI integration cannot
   silently skip missing infrastructure
-  test minimum/latest pymodbus, install built wheels outside the source
   tree with the serial extra, build docs with warnings as errors on
   PRs, and require these gates before publishing ##
   `2.2.1 <https://github.com/yoch/modpoll2mqtt/compare/v2.2.0...v2.2.1>`__
   (2026-06-16)

Internal
~~~~~~~~

-  add strict static typing with basedpyright across ``modpoll/``;
   introduce ``modpoll/types.py`` and enforce ``make typecheck`` in
   local and CI quality gates
-  extract ``Device``, ``Poller``, and ``Reference`` into
   ``modpoll/modbus_models.py`` (orchestration remains in
   ``modbus_task.py``)
-  replace black with ruff for format and lint

Documentation
~~~~~~~~~~~~~

-  add typing audit baseline in ``docs/typing-audit.md``
-  extend contributor release checklist and harden the
   ``on-release-main`` workflow

Tests
~~~~~

-  import device models from ``modpoll.modbus_models`` in unit tests

.. _section-2:

`2.2.0 <https://github.com/yoch/modpoll2mqtt/compare/v2.1.2...v2.2.0>`__ (2026-06-16)
-------------------------------------------------------------------------------------

.. _features-1:

Features
~~~~~~~~

-  **MQTT on-demand read (get):** subscribe on ``modpoll/+/get``
   (``--mqtt-get-topic-pattern``); publish ``{"ref": null}`` to
   ``modpoll/{device}/get``; response on
   ``modpoll/{device}/get/response`` with partial success (unknown or
   failed refs omitted); targeted Modbus reads via ``reference_read.py``
   with adjacent register batching
-  **Persistent Modbus connection:** ``ModbusConnectionManager`` keeps
   the client open between poll cycles and MQTT commands; non-blocking
   exponential reconnect backoff (``--modbus-backoff-base``,
   ``--modbus-backoff-max``); optional connection recycle
   (``--modbus-max-connection-age``)
-  enrich per-device MQTT diagnostics with ``config_source``, get/set
   counters (``get_count``, ``get_errors``, ``get_success``,
   ``get_unknown_refs``, ``get_read_errors``, ``set_count``,
   ``set_errors``, ``set_success``, ``set_unknown_refs``)
-  publish process-wide health on ``modpoll/diagnostics``
   (``mqtt_connected``, ``modbus_ok``, ``devices_failing``,
   ``last_cycle_s``, Modbus connection state and reconnect metrics)
-  publish process presence on ``modpoll/status`` with last-will
   (``online: false``) and birth message (``online: true``); status is
   always retained
-  apply ``--mqtt-retain`` to diagnostics topics (same policy as data
   publishes)

.. _breaking-changes-1:

BREAKING CHANGES
~~~~~~~~~~~~~~~~

-  **``--interval`` default is now transport-aware:** ``0.0`` s for
   TCP/UDP (was ``0.5`` s); serial/RTU derives the RTU 3.5-character
   frame gap from ``--serial-baud`` with a ``0.005`` s floor. Set
   ``--interval`` explicitly for slow devices.
-  **Modbus connect/close per poll cycle removed:** the client stays
   open until shutdown or a transport failure; on serial/RTU the port
   remains reserved while ``modpoll`` runs
-  **MQTT multi-reference writes** now use ``--interval`` between
   successive refs in one ``set`` message (was a fixed ``0.1`` s delay)

.. _documentation-1:

Documentation
~~~~~~~~~~~~~

-  document MQTT get, persistent connection, transport-aware
   ``--interval``, and extended diagnostics in ``docs/usage.rst``

.. _tests-1:

Tests
~~~~~

-  add ``tests/test_modbus_connection.py``, extend
   ``tests/test_main_get.py`` for backoff, connection reuse, and
   diagnostic counters
-  migrate Modbus lifecycle tests to ``ModbusConnectionManager``

.. _section-3:

`2.1.2 <https://github.com/yoch/modpoll2mqtt/compare/v2.1.1...v2.1.2>`__ (2026-06-15)
-------------------------------------------------------------------------------------

.. _features-2:

Features
~~~~~~~~

-  warn at config load when a reference is marked ``w``/``rw`` on a
   ``discrete_input`` or ``input_register`` poller (Modbus inputs are
   read-only)

.. _documentation-2:

Documentation
~~~~~~~~~~~~~

-  fix README MQTT write example for ``bool8`` references (array of 8
   booleans, not a scalar)
-  document export, diagnostics, mqtt-single, config sources, write
   constraints, and publish behavior in ``docs/usage.rst``
-  expand ``docs/configure.rst`` with CSV schema, dtypes, poll limits,
   and validation rules
-  correct ``config_template.csv`` comment: dtype column is required

.. _tests-2:

Tests
~~~~~

-  add contract tests in ``tests/test_handler_behavior.py`` for
   documented edge cases
-  consolidate handler regression/contract tests; share
   ``FakeModbusMaster`` in ``tests/helpers/modbus.py``

.. _section-4:

`2.1.1 <https://github.com/yoch/modpoll2mqtt/compare/v2.1.0...v2.1.1>`__ (2026-06-11)
-------------------------------------------------------------------------------------

.. _features-3:

Features
~~~~~~~~

-  add ``--mqtt-retain`` to set the MQTT retain flag on data publishes
   (diagnostics topics are never retained)

.. _documentation-3:

Documentation
~~~~~~~~~~~~~

-  add release documentation checklist for agents and maintainers in
   CONTRIBUTING.md
-  align narrative docs with MQTT reference-map write format and
   ``--mqtt-keys`` usage

.. _internal-1:

Internal
~~~~~~~~

-  centralize MQTT data publish policy (QoS and retain) in
   ``MqttHandler.publish_data_message``

.. _section-5:

`2.1.0 <https://github.com/yoch/modpoll2mqtt/compare/v2.0.0...v2.1.0>`__ (2026-06-10)
-------------------------------------------------------------------------------------

.. _features-4:

Features
~~~~~~~~

-  add ``--mqtt-keys name-only`` to publish MQTT JSON keys without the
   unit suffix (default key style remains ``name-with-unit``)
-  MQTT writes on ``modpoll/{device}/set`` accept a map of references
   ``{"ref_a": val, "ref_b": val}`` in one message; unknown keys are
   skipped with a warning

.. _breaking-changes-2:

BREAKING CHANGES
~~~~~~~~~~~~~~~~

-  renamed ``--daemon`` / ``-d`` to ``--no-output`` (suppresses poll
   result tables only; does not fork)
-  MQTT write payload must be a reference map (``{"ref": val}``);
   ``ref``/``value`` object format removed

.. _section-6:

`2.0.0 <https://github.com/yoch/modpoll2mqtt/compare/v1.6.0...v2.0.0>`__ (2026-06-10)
-------------------------------------------------------------------------------------

Project
~~~~~~~

-  forked from `modpoll <https://github.com/gavinying/modpoll>`__; PyPI
   package renamed to ``modpoll2mqtt``, repository ``yoch/modpoll2mqtt``
-  CLI command and Python module remain ``modpoll``

.. _features-5:

Features
~~~~~~~~

-  semantic MQTT write by CSV reference on ``modpoll/{device}/set`` with
   payload ``ref`` and ``value`` (device from topic; scale, dtype, and
   endianness handled automatically)
-  subscribe pattern ``modpoll/+/set`` by default

.. _breaking-changes-3:

BREAKING CHANGES
~~~~~~~~~~~~~~~~

-  removed low-level MQTT write format (``object_type``, ``address``,
   ``value``); use topic + ``ref`` and ``value`` instead
-  duplicate reference names on the same device now abort config loading
   (previously warned and overwrote)
