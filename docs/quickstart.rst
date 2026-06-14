Quickstart
==========

**modpoll2mqtt** communicates with Modbus devices using CSV configuration files. For the examples below, replace the TCP address with your device IP (or use ``examples/modsim.csv`` against a local Modbus TCP simulator).

Poll once
---------

.. code-block:: shell

    modpoll --once --tcp 192.168.1.10 --config examples/modsim.csv

Publish to MQTT
---------------

.. code-block:: shell

    modpoll --tcp 192.168.1.10 --mqtt-host broker.emqx.io --config examples/modsim.csv

With successful polling and publishing, subscribe to ``modpoll/{device_name}/data`` on the same broker to view the data.

Add ``--mqtt-retain`` so the broker keeps the last data message per topic for new subscribers (diagnostics are never retained). See :doc:`usage` for caveats.

Write by reference
------------------

Publish to ``modpoll/{device}/set``:

.. code-block:: json

    {
      "holding_reg01": 42
    }

See :doc:`usage` for details on MQTT topics and write semantics.
