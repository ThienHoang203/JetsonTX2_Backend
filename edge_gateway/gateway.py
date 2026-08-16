"""
EdgeGateway: điều phối MQTT local (ESP32) <-> MQTT cloud (backend) <-> AI worker process.
"""
import copy
import json
import multiprocessing as mp
import os
import queue
import threading
import time
from datetime import datetime

import httpx
import paho.mqtt.client as mqtt

from . import config
from .ai_worker import ai_worker_process
from .logging_setup import log
from .vibration_evaluator import VibrationWindowEvaluator


class EdgeGateway:
    def __init__(self):
        self.config_cache = {}
        self.config_lock = threading.Lock()

        # Bộ kiểm tra vượt ngưỡng độ rung theo cửa sổ trượt 10s từ Backend cũ
        self.vibration_evaluator = VibrationWindowEvaluator(
            window_size_sec=config.VIBRATION_WINDOW_SIZE_SEC,
            alert_min_count=config.VIBRATION_ALERT_MIN_COUNT,
            alert_min_duration_sec=config.VIBRATION_ALERT_MIN_DURATION_SEC,
            episode_reset_gap_sec=config.VIBRATION_EPISODE_RESET_GAP_SEC,
        )

        # Debounce theo từng node. busy_nodes lưu {node_id: trigger_timestamp}
        # để watchdog có thể force-release nếu quá lâu không có kết quả trả về.
        self.trigger_lock = threading.Lock()
        self.busy_nodes = {}

        self.upload_queue = queue.Queue(maxsize=config.UPLOAD_QUEUE_MAXSIZE)
        self.trigger_q = mp.Queue(maxsize=5)  # Giới hạn queue để tránh dồn ứ
        self.result_q = mp.Queue()

        self.local_mqtt = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1, client_id=f"{config.GATEWAY_ID}_Local"
        )
        self.local_mqtt.on_connect = self.on_local_connect
        self.local_mqtt.on_message = self.on_local_message

        self.cloud_mqtt = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1, client_id=f"{config.GATEWAY_ID}_Cloud"
        )
        self.cloud_mqtt.on_connect = self.on_cloud_connect
        self.cloud_mqtt.on_message = self.on_cloud_message

        self.ai_process = None

    # ---------- helper: publish cloud MQTT có kiểm tra kết quả ----------
    def safe_cloud_publish(self, topic: str, payload: str):
        """Publish lên cloud MQTT, log rõ khi thất bại thay vì âm thầm mất dữ liệu."""
        try:
            info = self.cloud_mqtt.publish(topic, payload)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                log.warning(f"Cloud MQTT publish thất bại (rc={info.rc}) topic={topic}")
        except Exception as e:
            log.error(f"Cloud MQTT publish exception topic={topic}: {e}")

    # ---------- watchdog: force-release busy_nodes bị kẹt & kiểm tra AI process ----------
    def watchdog_loop(self):
        while True:
            time.sleep(5)
            now = time.time()
            with self.trigger_lock:
                stuck = [
                    n for n, ts in self.busy_nodes.items()
                    if now - ts > config.BUSY_NODE_TIMEOUT_SEC
                ]
                for n in stuck:
                    log.warning(
                        f"Node {n} busy quá {config.BUSY_NODE_TIMEOUT_SEC}s không có kết quả AI, force-release."
                    )
                    del self.busy_nodes[n]

            if self.ai_process is not None and not self.ai_process.is_alive():
                log.error("AI worker process đã chết, khởi động lại...")
                self.ai_process = mp.Process(
                    target=ai_worker_process,
                    args=(self.trigger_q, self.result_q),
                    daemon=True,
                )
                self.ai_process.start()

    # ---------- config ----------
    def load_mock_config(self):
        with self.config_lock:
            self.config_cache = copy.deepcopy(config.DEFAULT_CONFIG)
        log.info("Loaded MOCK configuration from config.py for local testing.")

    def fetch_initial_config(self):
        try:
            log.info(f"OUT -> GET {config.BACKEND_API_URL}/gateway/{config.GATEWAY_ID}/config")
            resp = httpx.get(
                f"{config.BACKEND_API_URL}/gateway/{config.GATEWAY_ID}/config", timeout=10.0
            )
            if resp.status_code == 200:
                new_config = resp.json()
                with self.config_lock:
                    self.config_cache = new_config
                nodes = new_config.get("nodes", {})
                cameras = new_config.get("cameras", {})
                log.info(
                    f"Initial configuration synced from Backend ({len(nodes)} nodes, {len(cameras)} cameras)."
                )
                log.info(f"Config IN <- {new_config}")
            else:
                log.warning(
                    f"Failed to fetch config. Status: {resp.status_code}. Body: {resp.text[:300]!r}. Using mock."
                )
                self.load_mock_config()
        except Exception as e:
            log.warning(f"HTTP connection error: {e}. Using mock config.")
            self.load_mock_config()

    # ---------- upload worker (I/O bound) ----------
    def evidence_upload_worker(self):
        while True:
            task = self.upload_queue.get()
            if task is None:
                break
            local_path, gateway_id, confidence, timestamp, event_id = task
            log.info(f"Upload task IN <- event={event_id} path={local_path} confidence={confidence}")
            if not local_path:
                # AI worker báo lỗi/không có ảnh -> không có gì để upload
                self.upload_queue.task_done()
                continue
            try:
                with open(local_path, "rb") as f:
                    files = {"file": (os.path.basename(local_path), f, "image/jpeg")}
                    data = {
                        "event_id": event_id,
                        "gateway_id": gateway_id,
                        "confidence": str(confidence),
                        "timestamp": timestamp,
                    }
                    resp = httpx.post(
                        f"{config.BACKEND_API_URL}/evidence/upload",
                        files=files,
                        data=data,
                        timeout=20.0,
                    )
                    if resp.status_code in (200, 201):
                        log.info(f"Upload evidence success for event {event_id} (status={resp.status_code}).")
                    else:
                        log.error(f"Backend rejected upload event={event_id} status={resp.status_code}: {resp.text[:300]!r}")
            except Exception as e:
                log.error(f"Failed to upload event={event_id} path={local_path}: {e}")
            finally:
                if os.path.exists(local_path):
                    os.remove(local_path)
                self.upload_queue.task_done()

    # ---------- consumer đọc kết quả AI từ process riêng ----------
    def ai_result_consumer(self):
        while True:
            res = self.result_q.get()
            node_id = res["node_id"]
            event_id = res["event_id"]
            log.info(
                f"AI result IN <- node={node_id} event={event_id} ai_failed={res.get('ai_failed')} "
                f"severity={res.get('severity')} crack_detected={res.get('crack_detected')} "
                f"confidence={res.get('confidence')} error={res.get('error')}"
            )

            # Luôn giải phóng busy_nodes trước tiên, kể cả khi AI lỗi,
            # để node có thể trigger lại ở lần vượt ngưỡng tiếp theo.
            with self.trigger_lock:
                self.busy_nodes.pop(node_id, None)

            if res.get("ai_failed"):
                log.error(f"AI xử lý thất bại cho node {node_id}, event {event_id}: {res.get('error')}")
                continue

            topic = f"events/gateway/{config.GATEWAY_ID}/anomaly"
            anomaly_payload = {
                "event_id": event_id,
                "gateway_id": config.GATEWAY_ID,
                "node_id": node_id,
                "camera_id": res["camera_id"],
                "severity": res["severity"],
                "measured_val": res["measured_val"],
                "duration_sec": res["duration_sec"],
                "crack_detected": res["crack_detected"],
                "confidence": res["confidence"],
                "crack_size": res["crack_size"],
                "timestamp": res["timestamp"],
            }
            self.safe_cloud_publish(topic, json.dumps(anomaly_payload))
            log.info(f"Cloud MQTT OUT -> {topic} (Severity: {res['severity']}, Crack: {res['crack_detected']})")
            log.info(f"MQTT OUT -> {topic} {anomaly_payload}")

            try:
                self.upload_queue.put_nowait(
                    (
                        res["image_path"],
                        config.GATEWAY_ID,
                        res["confidence"],
                        res["timestamp"],
                        event_id,
                    )
                )
            except queue.Full:
                log.warning(f"Upload queue đầy, bỏ ảnh event {event_id} (backend có thể đang chậm/down).")
                if res["image_path"] and os.path.exists(res["image_path"]):
                    os.remove(res["image_path"])

    # ---------- local mqtt (ESP32) ----------
    def on_local_connect(self, client, userdata, flags, rc):
        log.info("Local MQTT connected. Subscribing to local sensors...")
        client.subscribe("local/node/+/sensor/+")

    def on_local_message(self, client, userdata, msg):
        try:
            payload_str = msg.payload.decode()
            topic_parts = msg.topic.split("/")
            if len(topic_parts) < 5:
                return

            node_id = topic_parts[2]
            sensor_type = topic_parts[4]
            payload = json.loads(payload_str)
            log.info(f"MQTT IN <- local topic={msg.topic} node={node_id} sensor={sensor_type} payload={payload_str}")

            # 1. Chuyển tiếp (Relay) dữ liệu cảm biến thô lên Cloud MQTT cho Backend vẽ biểu đồ thời gian thực
            cloud_topic = f"telemetry/gateway/{config.GATEWAY_ID}/node/{node_id}/{sensor_type}"
            self.safe_cloud_publish(cloud_topic, payload_str)

            # 2. Xử lý logic kiểm tra vượt ngưỡng độ rung
            if sensor_type == "vibration":
                raw_value = payload.get("value", payload.get("amp"))
                if raw_value is None:
                    log.warning(
                        f"Node {node_id} gửi payload vibration thiếu field 'value'/'amp', bỏ qua: {payload_str}"
                    )
                    return
                vibration_value = float(raw_value)
                now_ts = time.time()

                # Trích xuất cấu hình ngưỡng từ cache
                with self.config_lock:
                    node_config = self.config_cache.get("nodes", {}).get(node_id)
                    if node_config is None:
                        log.warning(f"Node {node_id} chưa có config, bỏ qua đánh giá ngưỡng.")
                        return
                    warn_high = float(node_config.get("warn_high", 2.5))
                    alert_high = float(node_config.get("alert_high", node_config.get("threshold", 15.0)))
                    critical_high = float(node_config.get("critical_high", 25.0))
                    alert_min_count = int(
                        node_config.get("alert_min_count", config.VIBRATION_ALERT_MIN_COUNT)
                    )
                    alert_min_duration_sec = float(
                        node_config.get("alert_min_duration_sec", config.VIBRATION_ALERT_MIN_DURATION_SEC)
                    )
                    episode_reset_gap_sec = float(
                        node_config.get("episode_reset_gap_sec", config.VIBRATION_EPISODE_RESET_GAP_SEC)
                    )

                    camera_id = node_config.get("camera_id")
                    camera_config = self.config_cache.get("cameras", {}).get(camera_id) if camera_id else None

                # ĐÁNH GIÁ NGƯỠNG: mật độ mẫu HOẶC thời lượng đợt rung, cái nào tới trước
                breach, severity, duration_sec = self.vibration_evaluator.evaluate(
                    node_id=node_id,
                    ppv=vibration_value,
                    timestamp_sec=now_ts,
                    warn_high=warn_high,
                    alert_high=alert_high,
                    critical_high=critical_high,
                    alert_min_count=alert_min_count,
                    alert_min_duration_sec=alert_min_duration_sec,
                    episode_reset_gap_sec=episode_reset_gap_sec,
                )
                log.info(
                    f"Evaluate result -> node={node_id} value={vibration_value} severity={severity} "
                    f"breach={breach} duration_sec={duration_sec:.1f}"
                )

                # Luôn publish trạng thái ngưỡng (kể cả chưa breach) để dashboard/backend
                # theo dõi realtime, không chỉ raw sensor value.
                status_topic = f"status/gateway/{config.GATEWAY_ID}/node/{node_id}/vibration"
                status_payload = {
                    "node_id": node_id,
                    "severity": severity,
                    "value": vibration_value,
                    "duration_sec": duration_sec,
                    "breach": breach,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                self.safe_cloud_publish(status_topic, json.dumps(status_payload))
                log.info(f"MQTT OUT -> {status_topic} {status_payload}")

                if not breach:
                    return

                # Vi phạm ngưỡng (ALERT đủ mật độ hoặc CRITICAL) -> cần ảnh để xác nhận
                if camera_config is None:
                    log.warning(
                        f"Node {node_id} breach ({severity}) nhưng thiếu camera_config, không thể chụp ảnh."
                    )
                    return

                with self.trigger_lock:
                    if node_id in self.busy_nodes:
                        log.info(f"Node {node_id} busy processing previous trigger, dropping new trigger.")
                        return
                    self.busy_nodes[node_id] = now_ts

                try:
                    self.trigger_q.put_nowait(
                        (node_id, vibration_value, severity, duration_sec, node_config, camera_config)
                    )
                    log.info(f"Triggered AI Worker for node {node_id} (Severity: {severity}, Value: {vibration_value} mm/s)")
                except queue.Full:
                    log.warning("AI trigger queue full, dropping trigger.")
                    with self.trigger_lock:
                        self.busy_nodes.pop(node_id, None)

        except Exception as e:
            log.error(f"Error processing local message: {e}")

    # ---------- cloud mqtt (backend) ----------
    def on_cloud_connect(self, client, userdata, flags, rc):
        log.info("Cloud MQTT connected. Subscribing to config updates...")
        client.subscribe(f"config/gateway/{config.GATEWAY_ID}/update")

    def on_cloud_message(self, client, userdata, msg):
        try:
            payload_str = msg.payload.decode()
            log.info(f"MQTT IN <- cloud topic={msg.topic} payload={payload_str}")
            if msg.topic == f"config/gateway/{config.GATEWAY_ID}/update":
                new_config = json.loads(payload_str)
                with self.config_lock:
                    self.config_cache = new_config
                nodes = new_config.get("nodes", {})
                cameras = new_config.get("cameras", {})
                log.info(
                    f"Configuration cache updated from Cloud ({len(nodes)} nodes, {len(cameras)} cameras)."
                )
        except Exception as e:
            log.error(f"Error processing config update: {e}")

    # ---------- run ----------
    def run(self):
        log.info(f"--- Starting Edge Inference Service for {config.GATEWAY_ID} ---")
        self.fetch_initial_config()

        threading.Thread(target=self.evidence_upload_worker, daemon=True).start()
        threading.Thread(target=self.ai_result_consumer, daemon=True).start()
        threading.Thread(target=self.watchdog_loop, daemon=True).start()

        self.ai_process = mp.Process(
            target=ai_worker_process, args=(self.trigger_q, self.result_q), daemon=True
        )
        self.ai_process.start()

        while True:
            try:
                self.cloud_mqtt.connect(config.CLOUD_MQTT_HOST, config.CLOUD_MQTT_PORT, 60)
                self.cloud_mqtt.loop_start()
                break
            except Exception as e:
                log.warning(f"Failed to connect Cloud MQTT, retry in 5s... ({e})")
                time.sleep(5)

        while True:
            try:
                self.local_mqtt.connect(config.LOCAL_MQTT_HOST, config.LOCAL_MQTT_PORT, 60)
                self.local_mqtt.loop_forever()
            except KeyboardInterrupt:
                log.info("Shutting down Edge Gateway...")
                self.local_mqtt.disconnect()
                self.cloud_mqtt.disconnect()
                self.trigger_q.put(None)
                break
            except Exception as e:
                log.warning(f"Local Mosquitto not ready, retry in 5s... ({e})")
                time.sleep(5)
