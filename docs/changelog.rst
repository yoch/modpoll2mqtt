Changelog
=========

[Unreleased]
------------

`2.2.0 <https://github.com/yoch/modpoll2mqtt/compare/v2.1.2...v2.2.0>`__ (2026-06-16)
-------------------------------------------------------------------------------------

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

Documentation
~~~~~~~~~~~~~

-  document MQTT get, persistent connection, transport-aware
   ``--interval``, and extended diagnostics in ``docs/usage.rst``

Tests
~~~~~

-  add ``tests/test_modbus_connection.py``, extend
   ``tests/test_main_get.py`` for backoff, connection reuse, and
   diagnostic counters
-  migrate Modbus lifecycle tests to ``ModbusConnectionManager``

.. _section-1:

`2.1.2 <https://github.com/yoch/modpoll2mqtt/compare/v2.1.1...v2.1.2>`__ (2026-06-15)
-------------------------------------------------------------------------------------

.. _features-1:

Features
~~~~~~~~

-  warn at config load when a reference is marked ``w``/``rw`` on a
   ``discrete_input`` or ``input_register`` poller (Modbus inputs are
   read-only)

.. _documentation-1:

Documentation
~~~~~~~~~~~~~

-  fix README MQTT write example for ``bool8`` references (array of 8
   booleans, not a scalar)
-  document export, diagnostics, mqtt-single, config sources, write
   constraints, and publish behavior in ``docs/usage.rst``
-  expand ``docs/configure.rst`` with CSV schema, dtypes, poll limits,
   and validation rules
-  correct ``config_template.csv`` comment: dtype column is required

.. _tests-1:

Tests
~~~~~

-  add contract tests in ``tests/test_handler_behavior.py`` for
   documented edge cases
-  consolidate handler regression/contract tests; share
   ``FakeModbusMaster`` in ``tests/helpers/modbus.py``

.. _section-2:

`2.1.1 <https://github.com/yoch/modpoll2mqtt/compare/v2.1.0...v2.1.1>`__ (2026-06-11)
-------------------------------------------------------------------------------------

.. _features-2:

Features
~~~~~~~~

-  add ``--mqtt-retain`` to set the MQTT retain flag on data publishes
   (diagnostics topics are never retained)

.. _documentation-2:

Documentation
~~~~~~~~~~~~~

-  add release documentation checklist for agents and maintainers in
   CONTRIBUTING.md
-  align narrative docs with MQTT reference-map write format and
   ``--mqtt-keys`` usage

Internal
~~~~~~~~

-  centralize MQTT data publish policy (QoS and retain) in
   ``MqttHandler.publish_data_message``

.. _section-3:

`2.1.0 <https://github.com/yoch/modpoll2mqtt/compare/v2.0.0...v2.1.0>`__ (2026-06-10)
-------------------------------------------------------------------------------------

.. _features-3:

Features
~~~~~~~~

-  add ``--mqtt-keys name-only`` to publish MQTT JSON keys without the
   unit suffix (default key style remains ``name-with-unit``)
-  MQTT writes on ``modpoll/{device}/set`` accept a map of references
   ``{"ref_a": val, "ref_b": val}`` in one message; unknown keys are
   skipped with a warning

.. _breaking-changes-1:

BREAKING CHANGES
~~~~~~~~~~~~~~~~

-  renamed ``--daemon`` / ``-d`` to ``--no-output`` (suppresses poll
   result tables only; does not fork)
-  MQTT write payload must be a reference map (``{"ref": val}``);
   ``ref``/``value`` object format removed

.. _section-4:

`2.0.0 <https://github.com/yoch/modpoll2mqtt/compare/v1.6.0...v2.0.0>`__ (2026-06-10)
-------------------------------------------------------------------------------------

Project
~~~~~~~

-  forked from `modpoll <https://github.com/gavinying/modpoll>`__; PyPI
   package renamed to ``modpoll2mqtt``, repository ``yoch/modpoll2mqtt``
-  CLI command and Python module remain ``modpoll``

.. _features-4:

Features
~~~~~~~~

-  semantic MQTT write by CSV reference on ``modpoll/{device}/set`` with
   payload ``ref`` and ``value`` (device from topic; scale, dtype, and
   endianness handled automatically)
-  subscribe pattern ``modpoll/+/set`` by default

.. _breaking-changes-2:

BREAKING CHANGES
~~~~~~~~~~~~~~~~

-  removed low-level MQTT write format (``object_type``, ``address``,
   ``value``); use topic + ``ref`` and ``value`` instead
-  duplicate reference names on the same device now abort config loading
   (previously warned and overwrote)
