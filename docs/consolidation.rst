Operation and migration from 2.2.1
==================================

These behaviors apply to the unreleased consolidation changes. The package
version is unchanged until the maintainer prepares a release.

Configuration and codecs
------------------------

Run ``modpoll --check-config --config a.csv b.csv`` before deploying. Every
source is validated as one set before transport startup. An invalid row aborts
the run with a source/line error. A reference belongs to its declared poller,
including function code and endian, even when ranges overlap. Identical pollers
reuse their block; different reference names can alias the same address.

Addresses are literal protocol addresses in 0..65535. There is no conversion
from a vendor's 1-based numbering or 40001-style labels. Device ID 0/broadcast is
unsupported. TCP/UDP allows IDs 1..255; serial allows 1..247.

Numeric bounds are checked before I/O. Unscaled integers require integral
values. Scaled integer writes divide by scale and use Python's rounding
(nearest, ties to even); reads multiply by scale. Scale must be finite/nonzero.
Floats must fit the selected IEEE representation and be finite. ``stringN``
accepts N UTF-8 bytes and reserves ``ceil(N/2)`` registers, padding with zeros.
It never truncates a multi-byte character.

For register bits, ``address:bit`` selects bit 0..15 of the decoded 16-bit word.
``bool8`` and ``bool16`` both occupy one register. Coil groups retain their
legacy convention: group address ``A`` in a poller starting at ``S`` maps to
physical coil ``S + (A-S)*8``; ``bool16`` spans two such groups. Incomplete
read groups are padded with false and marked ``partial``. Writing an incomplete
group is rejected; writing a complete group touches only its 8/16 coils.

Writes and acknowledgements
---------------------------

``--read-only`` disables legacy SET subscriptions and rejects versioned SET,
as well as handler writes. GET respects the CSV ``r`` permission. Protocol
input objects remain read-only regardless of CSV permissions.

Every known value in a SET is validated before any bus I/O. Overlapping writes
in the same command are rejected, except distinct bits in one register.
Legacy commands skip unknown names and report them; versioned SET rejects a
command containing any unknown reference. Several Modbus requests are not an
atomic transaction and are never rolled back.

``--modbus-bit-write read-modify-write`` is the default for register bits; it
preserves other bits but is non-atomic against other bus masters. Opt in to
``--modbus-bit-write mask`` only for devices implementing FC22. It performs an
atomic mask request and never silently falls back when unsupported. Pymodbus
request retries are disabled, so an uncertain write is not automatically replayed.

A SET result is one of:

.. list-table::
   :header-rows: 1

   * - Status
     - Meaning
   * - ``confirmed``
     - The device returned a successful response; this does not prove physical actuation.
   * - ``rejected``
     - Invalid value, permissions, object, or command; no request issued for it.
   * - ``failed``
     - A device exception response or local operation failure.
   * - ``not_attempted``
     - Skipped because validation failed elsewhere, expiry, shutdown, or no transport.
   * - ``unknown``
     - Transport failed while the write was in progress; it may have reached the device.

After ``unknown``, inspect device state before deciding whether another write
is appropriate. There is no guarantee of exactly-once actuation.

Versioned command protocol
--------------------------

Default topic: ``modpoll/<device>/commands``. Customize with
``--mqtt-command-topic-pattern`` (one complete ``+`` segment). The legacy
``/set`` and ``/get`` maps remain supported.

.. code-block:: json

    {
      "version": 1,
      "request_id": "set-0001",
      "operation": "set",
      "values": {"setpoint": 21.5},
      "expires_at": 2000000000
    }

GET uses ``"operation": "get", "references": ["setpoint"]``. Each envelope
requires version 1 and a request ID of 1..128 characters. ``expires_at`` is an
optional finite UTC Unix timestamp, defaulting to now + ``--mqtt-command-ttl``.
Expired requests are rejected, and SET checks expiry again before each
reference after connecting. Unknown envelope fields, duplicate JSON keys,
malformed UTF-8, unsupported values, and NaN/Infinity are rejected.

Responses go to the original request topic plus ``/response`` and always have
``retain=false``. Versioned responses include version, request ID, operation,
``deduplication_scope: process`` and ``results`` by reference. Legacy GET keeps
its map response, omitting unreadable references. Legacy SET returns ``results``.

An identical request ID and byte-identical payload returns its cached result.
A different payload with that ID is rejected. The cache holds at most 1000
results and 8 MiB; entries expire after
``max(300, --mqtt-command-ttl)`` seconds unless evicted earlier for capacity.
Restarting the process clears the cache. A cached result describes the original
execution and does not assert the current value. Publisher formatting must be
stable if it retries a request.

All incoming retained commands are discarded. Queue admission is distinct from
MQTT delivery acknowledgement: the broker's PUBACK does not mean execution.
Small versioned requests rejected for retention, queue saturation, or queue
expiry receive a best-effort rejection response. Oversized/malformed requests
and rejections beyond the bounded rejection queue can be dropped with counters.
Command responses themselves can be lost during a broker outage.

Resources, scheduling, and recovery
-----------------------------------

``--mqtt-max-payload-bytes`` (65536) bounds one command;
``--mqtt-rx-queue-size`` (1000) bounds queued commands and
``--mqtt-queue-bytes`` (1048576) bounds their total payload size.
A separate bounded rejection queue stores only payloads up to 2048 bytes.
``--mqtt-command-ttl`` (30 seconds) also bounds residence in the incoming queue.
``--mqtt-tx-queue-size`` (1000) bounds outgoing pending publications. Diagnostics
expose dropped/expired messages and publish/subscription errors. These limits
are process memory limits, not durable storage or an offline telemetry backlog.

One owner loop performs all Modbus I/O. MQTT callbacks enqueue commands only;
Paho owns asynchronous broker connection/reconnection. Publication while
disconnected fails quickly and increments a counter. Polling continues during
broker outages. Poll deadlines, queue age, reconnect backoff, and recovery use
monotonic time; acquired values and command absolute expiry use UTC time.
An overrun schedules the next period from the end of the delayed cycle, avoiding
a burst of catch-up polls. ``--timeout`` bounds each Modbus request; no internal
pymodbus retries multiply write attempts. Shutdown remains subject to an active
transport operation and the network library's connection timeout.

``--autoremove`` counts only actual poll failures and disables a block after
three consecutive failures. It remains permanent by default. Add
``--poller-recover-after 60`` to probe disabled blocks after 60 seconds. A failed
probe counts as the next attempt; three failed probes can disable the block again.
A connection backoff does not increment unattempted block/device counters.

At every poll cycle, current values are invalidated before reading. A failed
reference cannot retain a stale current value. Valid siblings still publish.
Diagnostics include ``quality`` (``good``, ``partial``, ``unavailable``),
``last_success_at`` and ``last_attempt_at`` per reference; historical successful
values/times are preserved internally. Partial group padding is a compatibility
value, not an acquired false value for the missing bits. ``last_poll_success``
means some values are available; poll errors and per-reference quality reveal
partial failure. Retained telemetry at the broker may still be historical:
consumers must use freshness diagnostics, not process presence alone.

``--export`` writes all devices from all files to one JSON snapshot, using a
temporary file beside the destination, flush/fsync and atomic replacement.
Unavailable references are null. ``--timestamp`` records the cycle timestamp
inside each device object. It is not a per-reference acquisition timestamp.
There is no promise of directory-fsync durability after a power loss.

MQTT deployment and lifecycle
-----------------------------

Use ``--mqtt-namespace site/gateway1`` for an isolated prefix on all default
patterns plus presence/global diagnostics. Explicit patterns outside the
``modpoll/`` prefix keep their chosen values. Multiple processes should use
separate namespaces and client IDs. Grant MQTT ACLs only for the required data,
command, response and diagnostic topics; retained commands are prohibited.

Sessions are ephemeral by default at every QoS. Set an explicit
``--mqtt-clientid`` and ``--mqtt-session-expiry N`` to request persistence.
MQTT 5 uses N as Session Expiry Interval; MQTT 3.1.1 uses clean_session=false
(the broker controls expiry). ``--mqtt-use-tls`` uses default trusted roots or
``--mqtt-cacerts`` and TLS 1.2 minimum. TLS 1.0/1.1 are rejected.
``--mqtt-insecure`` disables certificate/hostname verification explicitly.
``--mqtt-pass-file`` reads a UTF-8 password file; it is exclusive with
``--mqtt-pass``. Restrict the file permissions and avoid credentials in command
arguments or configuration URLs. URL credentials/query strings are removed
from source diagnostics.

Presence is retained and republishes ``online=true`` on every broker reconnect.
Clean shutdown sends ``online=false``, flushes pending publications for up to
``--mqtt-shutdown-timeout`` (3 seconds), and disconnects. SIGINT/SIGTERM interrupt
waiting and restore signal handlers after cleanup. ``--once`` subscribes to no
commands, polls one cycle and waits for accepted MQTT publications. Exit codes
are 0 for success, 1 for runtime/poll/publication failure, and 2 for invalid
configuration/options. Daemon mode tolerates temporary outages while exposing
them through logs and diagnostics.

Validation and remaining field work
-----------------------------------

Regression tests cover the audit defects (poller binding and parser state,
numeric overflow, UTF-8 isolation, endian bit writes, stale decodes, retained
commands, backoff, widths/groups, atomic export, attempted failure counters,
reconnect presence, strict configuration/timing, and once exit codes).
``make ci`` runs quality/type/unit gates. ``make test-integration`` uses a local
Mosquitto broker and a Modbus simulator with independent slaves and object
spaces; MQTT 5 and 3.1.1 are exercised by the real CLI. Set
``MODPOLL_REQUIRE_INTEGRATION=1`` to fail instead of skipping absent prerequisites.
CI tests pymodbus 3.10.0 and the latest compatible release, installs built wheels
outside the source checkout (including the serial extra), and gates PR/release
docs builds with warnings as errors.

Before deploying to physical equipment, verify serial/UDP timing, actual FC22
support, endian maps against vendor examples, competing-master behavior,
certificate/authentication failure, and a multi-day outage/recovery run. Local
simulators cannot establish those hardware-specific guarantees.
