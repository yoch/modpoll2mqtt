Changelog
=========

`2.1.1 <https://github.com/yoch/modpoll2mqtt/compare/v2.1.0...v2.1.1>`__ (2026-06-11)
-------------------------------------------------------------------------------------

Features
~~~~~~~~

-  add ``--mqtt-retain`` to set the MQTT retain flag on data publishes
   (diagnostics topics are never retained)

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

.. _section-1:

`2.1.0 <https://github.com/yoch/modpoll2mqtt/compare/v2.0.0...v2.1.0>`__ (2026-06-10)
-------------------------------------------------------------------------------------

.. _features-1:

Features
~~~~~~~~

-  add ``--mqtt-keys name-only`` to publish MQTT JSON keys without the
   unit suffix (default key style remains ``name-with-unit``)
-  MQTT writes on ``modpoll/{device}/set`` accept a map of references
   ``{"ref_a": val, "ref_b": val}`` in one message; unknown keys are
   skipped with a warning

BREAKING CHANGES
~~~~~~~~~~~~~~~~

-  renamed ``--daemon`` / ``-d`` to ``--no-output`` (suppresses poll
   result tables only; does not fork)
-  MQTT write payload must be a reference map (``{"ref": val}``);
   ``ref``/``value`` object format removed

.. _section-2:

`2.0.0 <https://github.com/yoch/modpoll2mqtt/compare/v1.6.0...v2.0.0>`__ (2026-06-10)
-------------------------------------------------------------------------------------

Project
~~~~~~~

-  forked from `modpoll <https://github.com/gavinying/modpoll>`__; PyPI
   package renamed to ``modpoll2mqtt``, repository ``yoch/modpoll2mqtt``
-  CLI command and Python module remain ``modpoll``

.. _features-2:

Features
~~~~~~~~

-  semantic MQTT write by CSV reference on ``modpoll/{device}/set`` with
   payload ``ref`` and ``value`` (device from topic; scale, dtype, and
   endianness handled automatically)
-  subscribe pattern ``modpoll/+/set`` by default

.. _breaking-changes-1:

BREAKING CHANGES
~~~~~~~~~~~~~~~~

-  removed low-level MQTT write format (``object_type``, ``address``,
   ``value``); use topic + ``ref`` and ``value`` instead
-  duplicate reference names on the same device now abort config loading
   (previously warned and overwrote)
