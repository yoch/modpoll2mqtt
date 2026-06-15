Configuration
=============

Modbus configure file
---------------------

To communicate with Modbus devices, a CSV configure file describes device addresses, poll ranges, and reference mappings. See also :doc:`usage` for coil/register bit semantics and MQTT write constraints.

CSV structure
^^^^^^^^^^^^^

Each non-empty row is one of three types:

- **device** — ``device,<name>,<slave_id>``
- **poll** — ``poll,<object_type>,<start>,<size>,<endian>``
- **ref** — ``ref,<name>,<address>,<dtype>,<rw>,<unit>,<scale>`` (``<unit>`` and ``<scale>`` optional)

- **device** — ``<name>`` is unique per file (used in MQTT topics). ``<slave_id>`` is the Modbus unit ID (1–254). Multiple devices may share the same slave ID when they are logical views of one physical slave; poll ranges must not overlap.
- **poll** — ``<object_type>`` is ``coil``, ``discrete_input``, ``holding_register``, or ``input_register``. ``<start>`` and ``<size>`` define the address range. ``<endian>`` must be ``BE_BE``, ``LE_BE``, ``LE_LE``, or ``BE_LE``.
- **ref** — ``<address>`` may be decimal or hexadecimal (``0x10``). For register bit reads, use ``<address>:<bit>`` (0–15) with ``dtype`` ``bool``. ``<rw>`` is ``r``, ``w``, or ``rw``. ``<unit>`` and ``<scale>`` are optional; scale multiplies read values and divides write values.

Supported dtypes
^^^^^^^^^^^^^^^^

``uint16``, ``int16``, ``uint32``, ``int32``, ``uint64``, ``int64``, ``float16``, ``float32``, ``float64``, ``bool``, ``bool8``, ``bool16``, ``stringNNN`` (where ``NNN`` is the string byte length).

Poll size limits
^^^^^^^^^^^^^^^^

- Coils and discrete inputs: maximum **2000** points per poll.
- Holding and input registers: maximum **123** registers per poll.

Validation rules
^^^^^^^^^^^^^^^^

- Duplicate **device** names or **reference** names on the same device abort config loading.
- Duplicate pollers (same function code, start, size, endian on one device) are ignored with a warning.
- Overlapping poll ranges on the same slave ID and function code produce a warning.
- References outside the poller range or with invalid endian/dtype are ignored with a warning.
- References marked ``w`` or ``rw`` on ``discrete_input`` or ``input_register`` pollers log a warning at load time (Modbus inputs are read-only; MQTT writes are rejected).

Here is the annotated template:

.. literalinclude:: ../examples/config_template.csv
   :language: default
   :emphasize-lines: 8-10
   :linenos:


Example 1: Modsim device (Modbus TCP device)
----------------------------------------------

This example configuration is included for local testing with a Modbus TCP simulator or device.

.. literalinclude:: ../examples/modsim.csv
   :language: default
   :linenos:


Example 2: SCPM-S6 Power Meter (Modbus RTU device)
--------------------------------------------------

SCPM-S6 is designed as a sub-circuit power meter to monitor multiple electrical circuit power consumptions.

.. literalinclude:: ../contrib/eniwise/scpms6.csv
   :language: default
   :linenos:
