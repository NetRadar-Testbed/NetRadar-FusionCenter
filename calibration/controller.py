"""
NetRadar  –  Calibration controller
=================================
MQTT service that runs multi-node self-calibration **entirely from the
nodes' always-on live frame stream**.  Nodes are never asked to collect
or publish calibration data — calibration is fully decoupled from them.

Decoupled design
----------------
* Nodes continuously stream live frames to
  ``netradar/v1/group/<gid>/frames/<nid>`` whenever they are up (node
  ``socket_process``); no calibration-specific node code runs.
* Pressing "calibrate" on the dashboard publishes
  ``netradar/v1/server/start/request`` with ``captureConfig.calibration=true``.
* This controller arms a collection window and buffers the **next**
  ``calibrationFrames`` frames **per node** from the live stream, keyed by
  per-node arrival index.  All nodes capture on the same hardware pulse
  cadence (aligned by the time-sync service), so node *k*'s *k*-th frame is
  the same physical moment on every node.
* When every streaming node has contributed N frames (or a deadline
  expires) the controller runs the closed-form pairwise calibrator.

Flow
----
1. Dashboard publishes  netradar/v1/server/start/request   (calibration=true)
2. Controller arms a session — **no capture/start is sent to nodes**
3. Controller buffers the next N live frames per node from frames/<nid>
4. When all streaming nodes are done (or deadline) the controller runs
   closed-form calibration
5. Result published to  netradar/v1/group/<gid>/calibration/result
6. Result also persisted to Postgres
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import paho.mqtt.client as mqtt
import psycopg2
from calibration import closed_form_calibration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
TOPIC_PREFIX = os.getenv("NETRADAR_TOPIC_PREFIX", "netradar/v1")

START_REQUEST_TOPIC = f"{TOPIC_PREFIX}/server/start/request"
START_RESULT_TOPIC = f"{TOPIC_PREFIX}/server/start/result"
SERVER_STATUS_TOPIC = f"{TOPIC_PREFIX}/server/status"
FRAMES_TOPIC = f"{TOPIC_PREFIX}/group/+/frames/+"

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
MQTT_HOST = os.getenv("MQTT_HOST", "nanomq")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER") or os.getenv("MQTT_SERVER_USER") or None
MQTT_PASS = os.getenv("MQTT_PASS") or os.getenv("MQTT_SERVER_PASS") or None
CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "netradar-calibration")

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "netradar")
DB_USER = os.getenv("DB_USER", "netradar")
DB_PASS = os.getenv("DB_PASS")  # no default: set by setup.sh via .env

# Idle timeout: force a solve when no live frames at all arrive for this long.
CALIB_SESSION_TIMEOUT_MS = int(os.getenv("NETRADAR_CALIB_SESSION_TIMEOUT_MS", "30000"))
# Hard deadline for a collection window (e.g. a node stalls mid-window).
CALIB_COLLECT_TIMEOUT_MS = int(os.getenv("NETRADAR_CALIB_COLLECT_TIMEOUT_MS", "60000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CALIB-CTL] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("calibration.controller")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CalibrationSession:
    command_id: str
    group_id: str
    target_frames: int
    # node_id -> frame_idx -> list of {"track_id": int, "x": float, "y": float}
    frame_data: Dict[str, Dict[int, List]] = field(default_factory=dict)
    # nodes that have contributed at least one live frame since arming
    active_nodes: Set[str] = field(default_factory=set)
    created_ms: int = field(default_factory=_now_ms)
    last_activity_ms: int = field(default_factory=_now_ms)
    deadline_ms: int = field(default_factory=_now_ms)
    done: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def is_complete_locked(self) -> bool:
        """All nodes that are streaming have contributed target_frames frames."""
        if not self.active_nodes:
            return False
        return all(
            len(self.frame_data.get(nid, {})) >= self.target_frames
            for nid in self.active_nodes
        )


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class CalibrationController:
    def __init__(self) -> None:
        # Calibration sessions: commandId -> CalibrationSession
        self.sessions: Dict[str, CalibrationSession] = {}
        self.sessions_lock = threading.Lock()

        self.shutdown = threading.Event()
        self._watchdog_started = False

        # DB connection (lazy)
        self._db_conn = None

        # MQTT
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=CLIENT_ID,
            clean_session=False,
        )
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)

        # LWT — server status
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
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    # ----- DB helpers ---------------------------------------------------

    def _get_db(self):
        if self._db_conn is None or self._db_conn.closed:
            self._db_conn = psycopg2.connect(
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASS,
            )
            self._db_conn.autocommit = True
        return self._db_conn

    def _persist_result(self, payload: dict) -> None:
        try:
            conn = self._get_db()
            cur = conn.cursor()
            cur.execute(
                """CREATE TABLE IF NOT EXISTS calibration_results (
                    id          BIGSERIAL PRIMARY KEY,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    group_id    TEXT NOT NULL,
                    command_id  TEXT NOT NULL,
                    ok          BOOLEAN NOT NULL,
                    reason      TEXT,
                    node_ids    JSONB,
                    P_opt       JSONB,
                    theta_opt   JSONB,
                    solve_meta  JSONB,
                    raw_payload JSONB NOT NULL
                )""",
            )
            cur.execute(
                """INSERT INTO calibration_results
                   (group_id, command_id, ok, reason, node_ids, P_opt, theta_opt, solve_meta, raw_payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    payload.get("groupId"),
                    payload.get("commandId"),
                    payload.get("ok"),
                    payload.get("reason"),
                    json.dumps(payload.get("nodeIds")),
                    json.dumps(payload.get("P_opt")),
                    json.dumps(payload.get("theta_opt")),
                    json.dumps(payload.get("solveMeta") or {}),
                    json.dumps(payload),
                ),
            )
            cur.close()
            log.info(
                "Persisted calibration result to DB  commandId=%s",
                payload.get("commandId"),
            )
        except Exception:
            log.exception("Failed to persist result to DB")

    # ----- MQTT callbacks -----------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log.error("MQTT connect failed  rc=%s", reason_code)
            return

        subs = [
            START_REQUEST_TOPIC,
            FRAMES_TOPIC,
        ]
        for t in subs:
            client.subscribe(t, qos=1)
            log.info("Subscribed  %s", t)

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

        log.info("Calibration controller online  broker=%s:%s", MQTT_HOST, MQTT_PORT)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.shutdown.is_set():
            log.info("Disconnected from broker (shutdown)")
        else:
            log.warning("Unexpected disconnect  rc=%s", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode()) if msg.payload else {}
        except json.JSONDecodeError:
            return

        topic = msg.topic

        if topic == START_REQUEST_TOPIC:
            threading.Thread(
                target=self._handle_start_request, args=(payload,), daemon=True
            ).start()
        elif "/frames/" in topic:
            self._handle_live_frame(topic, payload)

    # ----- Start request → arm a collection window ---------------------

    def _handle_start_request(self, payload: dict) -> None:
        """Arm a calibration collection window.

        The controller never asks nodes for frames — it simply records the
        next ``calibrationFrames`` live frames per node and solves once every
        streaming node has contributed enough frames.
        """
        group_id = str(payload.get("groupId", "")).strip()
        capture_cfg = payload.get("captureConfig", {}) or {}
        is_calib = capture_cfg.get("calibration", False)

        if not group_id:
            self._publish_result(
                ok=False, reason="missing_group_id", request_id=payload.get("requestId")
            )
            return

        if not is_calib:
            self._publish_result(
                ok=False,
                reason="not_a_calibration_request",
                request_id=payload.get("requestId"),
                group_id=group_id,
            )
            log.warning(
                "Rejected non-calibration start request  group=%s  (use TimeSyncService)",
                group_id,
            )
            return

        try:
            target_frames = max(2, int(capture_cfg.get("calibrationFrames", 150)))
        except (TypeError, ValueError):
            target_frames = 150

        with self.sessions_lock:
            # One calibration at a time per group.
            for existing in self.sessions.values():
                if existing.group_id == group_id and not existing.done:
                    self._publish_result(
                        ok=False,
                        reason="calibration_in_progress",
                        request_id=payload.get("requestId"),
                        group_id=group_id,
                        extras={"commandId": existing.command_id},
                    )
                    log.info(
                        "Rejected calibration start  group=%s  (already running commandId=%s)",
                        group_id,
                        existing.command_id,
                    )
                    return

            now_ms = _now_ms()
            command_id = f"cmd-{uuid.uuid4().hex[:12]}"
            session = CalibrationSession(
                command_id=command_id,
                group_id=group_id,
                target_frames=target_frames,
                created_ms=now_ms,
                last_activity_ms=now_ms,
                deadline_ms=now_ms + CALIB_COLLECT_TIMEOUT_MS,
            )
            self.sessions[command_id] = session

        log.info(
            "Calibration armed  commandId=%s  group=%s  frames_per_node=%d  "
            "— collecting from live stream (no node involvement)",
            command_id,
            group_id,
            target_frames,
        )

        # Acknowledge the requester
        self._publish_result(
            ok=True,
            reason="calibration_armed",
            request_id=payload.get("requestId"),
            group_id=group_id,
            extras={
                "commandId": command_id,
                "calibrationFrames": target_frames,
                "collectTimeoutMs": CALIB_COLLECT_TIMEOUT_MS,
            },
        )

    def _publish_result(
        self,
        ok: bool,
        reason: str,
        request_id: Optional[str] = None,
        group_id: Optional[str] = None,
        extras: Optional[dict] = None,
    ) -> None:
        payload: dict = {
            "schemaVersion": SCHEMA_VERSION,
            "timestampMs": _now_ms(),
            "requestId": request_id,
            "groupId": group_id,
            "ok": ok,
            "reason": reason,
        }
        if extras:
            payload.update(extras)
        self.client.publish(START_RESULT_TOPIC, payload=json.dumps(payload), qos=1)
        log.info("Published result  ok=%s  reason=%s  group=%s", ok, reason, group_id)

    # ----- Live-frame collection ---------------------------------------

    def _handle_live_frame(self, topic: str, payload: dict) -> None:
        """Feed every live frame into any armed session for its group."""
        parts = topic.split("/")
        if len(parts) < 6:
            return
        group_id, node_id = parts[3], parts[-1]
        tracks = payload.get("tracks", [])

        with self.sessions_lock:
            sessions = [s for s in self.sessions.values() if s.group_id == group_id]

        for session in sessions:
            complete = False
            with session.lock:
                if session.done:
                    continue
                session.last_activity_ms = _now_ms()
                session.active_nodes.add(node_id)
                # Per-node arrival index: node *k*'s k-th frame is the same
                # physical pulse moment on every node (shared trigger clock).
                node_frames = session.frame_data.setdefault(node_id, {})
                node_frames[len(node_frames)] = tracks
                complete = session.is_complete_locked()
                if complete:
                    session.done = True

            if complete:
                log.info(
                    "Calibration collection complete  commandId=%s  nodes=%s  frames/node=%d",
                    session.command_id,
                    sorted(session.active_nodes),
                    session.target_frames,
                )
                threading.Thread(
                    target=self._run_calibration, args=(session,), daemon=True
                ).start()

    # ----- Calibration solver -------------------------------------------

    def _run_calibration(self, session: CalibrationSession) -> None:
        with session.lock:
            snapshot = {nid: dict(frames) for nid, frames in session.frame_data.items()}
            command_id = session.command_id
            group_id = session.group_id

        log.info(
            "Running calibration  commandId=%s  nodes=%s",
            command_id,
            sorted(snapshot.keys()),
        )

        node_ids, P_opt, theta_opt, solve_meta = closed_form_calibration(snapshot)

        result_topic = f"{TOPIC_PREFIX}/group/{group_id}/calibration/result"

        if node_ids is None:
            reason = (solve_meta or {}).get("reason", "calibration_failed")
            result = {
                "schemaVersion": SCHEMA_VERSION,
                "timestampMs": _now_ms(),
                "groupId": group_id,
                "commandId": command_id,
                "ok": False,
                "reason": reason,
                "nodeIds": sorted(snapshot.keys()),
                "solveMeta": solve_meta,
            }
        else:

            def _serialize_complex(mat):
                return [[[float(v.real), float(v.imag)] for v in row] for row in mat]

            result = {
                "schemaVersion": SCHEMA_VERSION,
                "timestampMs": _now_ms(),
                "groupId": group_id,
                "commandId": command_id,
                "ok": True,
                "reason": "calibration_complete",
                "nodeIds": node_ids,
                "P_opt": _serialize_complex(P_opt),
                "theta_opt": theta_opt.tolist(),
                "solveMeta": solve_meta,
            }

        self.client.publish(result_topic, payload=json.dumps(result), qos=1)
        log.info(
            "Calibration result published  commandId=%s  ok=%s  topic=%s",
            command_id,
            result["ok"],
            result_topic,
        )

        self._persist_result(result)

        with self.sessions_lock:
            self.sessions.pop(command_id, None)

    # ----- Session watchdog ---------------------------------------------

    def _watchdog_loop(self) -> None:
        """Periodically close sessions that never completed.

        A session is forced to completion when:
          - no live frames at all arrived for CALIB_SESSION_TIMEOUT_MS, or
          - the collection deadline (CALIB_COLLECT_TIMEOUT_MS) has passed.
        The solve then runs with whatever data has been collected.
        """
        poll_interval_s = max(1, min(CALIB_SESSION_TIMEOUT_MS, CALIB_COLLECT_TIMEOUT_MS) // 5000)
        log.info(
            "Watchdog started  idle_timeout_ms=%d  collect_timeout_ms=%d  poll_interval_s=%d",
            CALIB_SESSION_TIMEOUT_MS,
            CALIB_COLLECT_TIMEOUT_MS,
            poll_interval_s,
        )

        while not self.shutdown.wait(timeout=poll_interval_s):
            timed_out_sessions: list = []
            now_ms = _now_ms()

            with self.sessions_lock:
                for cmd_id, session in list(self.sessions.items()):
                    with session.lock:
                        if session.done:
                            continue
                        idle_ms = now_ms - session.last_activity_ms
                        if (
                            idle_ms >= CALIB_SESSION_TIMEOUT_MS
                            or now_ms >= session.deadline_ms
                        ):
                            session.done = True
                            timed_out_sessions.append((cmd_id, session))

            for cmd_id, session in timed_out_sessions:
                with session.lock:
                    received_nodes = sorted(session.frame_data.keys())
                    total_frames = sum(
                        len(frames) for frames in session.frame_data.values()
                    )
                log.warning(
                    "Calibration session closed by watchdog  commandId=%s  "
                    "idle_ms=%d  received_nodes=%s  total_frames=%d  done=%d/%d",
                    cmd_id,
                    now_ms - session.last_activity_ms,
                    received_nodes,
                    total_frames,
                    len(session.active_nodes),
                    len(session.active_nodes),
                )
                threading.Thread(
                    target=self._run_calibration, args=(session,), daemon=True
                ).start()

    def _ensure_watchdog(self) -> None:
        """Start the watchdog thread once, on first use."""
        if not self._watchdog_started:
            self._watchdog_started = True
            threading.Thread(target=self._watchdog_loop, daemon=True).start()

    # ----- Run ----------------------------------------------------------

    def run(self) -> None:
        # Wait for DB
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                c = self._get_db()
                c.close()
                self._db_conn = None
                log.info("Database ready")
                break
            except Exception:
                log.info("Waiting for database …")
                time.sleep(2)
        else:
            log.error("Database not ready; exiting")
            sys.exit(1)

        self._ensure_watchdog()
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        self.client.loop_start()

        log.info("Calibration controller running  clientId=%s", CLIENT_ID)
        self.shutdown.wait()

        self.client.loop_stop()
        self.client.disconnect()
        log.info("Calibration controller stopped")


if __name__ == "__main__":
    ctl = CalibrationController()
    # Graceful shutdown on SIGTERM / SIGINT
    import signal as _signal

    _signal.signal(_signal.SIGTERM, lambda *_: ctl.shutdown.set())
    _signal.signal(_signal.SIGINT, lambda *_: ctl.shutdown.set())
    ctl.run()
