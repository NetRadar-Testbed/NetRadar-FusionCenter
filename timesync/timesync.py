"""
NetRadar  –  Time Synchronisation Service
======================================
Periodically publishes capture/start commands (every 10 s) to keep every
node in each configured group time-aligned and continuously capturing frames.

This service is intentionally decoupled from calibration — nodes can
join the network, receive a time-sync start command, and begin hardware
frame collection immediately, without any calibration step.

Flow
----
1. Every 10 s, for each configured group:
   - Compute  startEpochMs = now + lead time
   - Publish   netradar/v1/group/<gid>/capture/start
     (captureConfig.calibration = false — purely a time reference)
2. Nodes pick up the next periodic command naturally — no presence
   tracking required.

Topic published
---------------
netradar/v1/group/<gid>/capture/start
  {
    "schemaVersion": 1,
    "timestampMs":    <server epoch ms>,
    "groupId":        "<gid>",
    "commandId":      "synch-<uuid>",
    "startEpochMs":   <future epoch ms>,
    "captureConfig": {
      "calibration": false
    }
  }
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
TOPIC_PREFIX = os.getenv("NETRADAR_TOPIC_PREFIX", "netradar/v1")

SERVER_STATUS_TOPIC = f"{TOPIC_PREFIX}/server/status"

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
MQTT_HOST = os.getenv("MQTT_HOST", "nanomq")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER") or os.getenv("MQTT_SERVER_USER") or None
MQTT_PASS = os.getenv("MQTT_PASS") or os.getenv("MQTT_SERVER_PASS") or None
CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "netradar-timesync")

LEAD_TIME_MS = int(os.getenv("NETRADAR_LEAD_TIME_MS", "5000"))
SYNC_INTERVAL_S = int(os.getenv("NETRADAR_SYNC_INTERVAL_S", "10"))

# Must equal the nodes' hardware-trigger pulse period (50 ms).
# Set TRIGGER_PULSE_PERIOD_MS=50 explicitly on every node so this stays coupled.
PULSE_PERIOD_MS = int(os.getenv("TRIGGER_PULSE_PERIOD_MS", "50"))

# Comma-separated list of group IDs to send time-sync ticks to.
# Defaults to "default" so it works out of the box.
SYNC_GROUPS = [
    g.strip()
    for g in os.getenv("NETRADAR_SYNC_GROUPS", "default").split(",")
    if g.strip()
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TIMESYNC] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("timesync")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _quantize_to_grid(ms: int) -> int:
    """Round `ms` up to the next pulse-period boundary.

    Guarantees startEpochMs % PULSE_PERIOD_MS == 0 for every tick, so every
    node — regardless of when it joins — fires on the same absolute phase.
    """
    return ((ms + PULSE_PERIOD_MS - 1) // PULSE_PERIOD_MS) * PULSE_PERIOD_MS


# ---------------------------------------------------------------------------
# Time Sync Service
# ---------------------------------------------------------------------------


class TimeSyncService:
    """Periodically sends time-reference capture/start commands.

    Publishes directly to the configured groups — no presence or
    node-state tracking needed.  Nodes simply pick up the next tick.
    """

    def __init__(self) -> None:
        self.shutdown = threading.Event()
        self._connected = threading.Event()

        # MQTT
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=CLIENT_ID,
            clean_session=False,
        )
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)

        # LWT
        self.client.will_set(
            SERVER_STATUS_TOPIC,
            payload=json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "serverId": CLIENT_ID,
                    "status": "offline",
                    "timestampMs": _now_ms(),
                }
            ),
            qos=1,
            retain=True,
        )

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    # ----- MQTT callbacks -------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log.error("MQTT connect failed  rc=%s", reason_code)
            return

        self._connected.set()

        client.publish(
            SERVER_STATUS_TOPIC,
            payload=json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "serverId": CLIENT_ID,
                    "status": "online",
                    "timestampMs": _now_ms(),
                }
            ),
            qos=1,
            retain=True,
        )

        log.info(
            "Time-sync service online  broker=%s:%s  interval=%ds  groups=%s",
            MQTT_HOST,
            MQTT_PORT,
            SYNC_INTERVAL_S,
            SYNC_GROUPS,
        )

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.shutdown.is_set():
            log.info("Disconnected (shutdown)")
        else:
            log.warning("Unexpected disconnect  rc=%s", reason_code)

    # ----- Periodic sync tick -------------------------------------------

    def _sync_tick(self) -> None:
        """One iteration: send a capture/start to every configured group."""
        start_epoch_ms = _quantize_to_grid(_now_ms() + LEAD_TIME_MS)

        for group_id in SYNC_GROUPS:
            command_id = f"synch-{uuid.uuid4().hex[:12]}"
            topic = f"{TOPIC_PREFIX}/group/{group_id}/capture/start"

            payload = {
                "schemaVersion": SCHEMA_VERSION,
                "timestampMs": _now_ms(),
                "groupId": group_id,
                "commandId": command_id,
                "startEpochMs": start_epoch_ms,
                "captureConfig": {
                    "calibration": False,
                },
            }

            info = self.client.publish(
                topic,
                payload=json.dumps(payload),
                qos=1,
            )
            log.info(
                "Synch tick  group=%s  commandId=%s  startEpochMs=%s  mod50=%s  rc=%s",
                group_id,
                command_id,
                start_epoch_ms,
                start_epoch_ms % PULSE_PERIOD_MS,   # must always be 0
                info.rc,
            )

    # ----- Run loop -----------------------------------------------------

    def _sync_loop(self) -> None:
        """Fire _sync_tick on a fixed wall-clock schedule (no cumulative drift)."""
        next_tick = time.monotonic()
        while not self.shutdown.is_set():
            self._sync_tick()
            next_tick += SYNC_INTERVAL_S
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                self.shutdown.wait(timeout=sleep_for)

    def run(self) -> None:
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        self.client.loop_start()

        # Wait for the MQTT connection before firing the first tick.
        if not self._connected.wait(timeout=10):
            log.error("MQTT connection timed out — exiting")
            sys.exit(1)

        log.info("Time-sync service running  clientId=%s", CLIENT_ID)

        sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        sync_thread.start()

        try:
            while not self.shutdown.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Shutdown requested")
        finally:
            self.shutdown.set()
            sync_thread.join(timeout=5)
            self.client.loop_stop()
            self.client.disconnect()
            log.info("Time-sync service stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    svc = TimeSyncService()
    svc.run()


if __name__ == "__main__":
    main()
