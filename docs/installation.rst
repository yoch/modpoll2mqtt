Installation
============

This program is tested on Python 3.10+ (Ubuntu 22.04+ or equivalent). Ubuntu 20.04 and Python 3.8 are no longer supported.

The PyPI package is named **modpoll2mqtt**; the installed command is **modpoll**.

Install with pip or pipx
-------------------------

.. code-block:: shell

    pip install modpoll2mqtt

Optionally, install pyserial for Modbus-RTU communication:

.. code-block:: shell

    pip install 'modpoll2mqtt[serial]'

Upgrade:

.. code-block:: shell

    pip install -U modpoll2mqtt

On Windows, ``pipx`` is recommended. Refer to the `pipx <https://pypa.github.io/pipx/installation/>`_ installation guide.

.. code-block:: shell

    pipx install modpoll2mqtt

.. code-block:: shell

    pipx upgrade modpoll2mqtt

Uninstall
---------

.. code-block:: shell

    pip uninstall modpoll2mqtt

or:

.. code-block:: shell

    pipx uninstall modpoll2mqtt
