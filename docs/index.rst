.. modpoll2mqtt documentation

.. meta::
    :description: Command-line Modbus RTU/TCP/UDP to MQTT gateway: polls devices from CSV register maps, publishes values to MQTT and writes registers by name. Fork of modpoll.

modpoll2mqtt documentation
==========================

**modpoll2mqtt** is a command-line Modbus-to-MQTT gateway. Install it with ``pip install modpoll2mqtt`` and run the ``modpoll`` command.

It polls Modbus devices (RTU, TCP, UDP) using CSV configuration files, publishes values to an MQTT broker, and supports semantic write commands by CSV reference name.

Package on `PyPI <https://pypi.org/project/modpoll2mqtt/>`__, source code and issues on `GitHub <https://github.com/yoch/modpoll2mqtt>`__.

.. toctree::
    :maxdepth: 2
    :caption: Contents:

    installation
    quickstart
    usage
    configure
    consolidation
    changelog

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
