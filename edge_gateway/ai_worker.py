"""
AI Worker chạy trong PROCESS riêng biệt (multiprocessing.Process), không share
GIL với main process, nhờ đó vòng lặp MQTT ở main process không bao giờ bị
nghẽn khi AI đang predict/capture ảnh.

QUAN TRỌNG: ai_worker_process phải là hàm module-level (không phải closure/method)
để multiprocessing với start method "spawn" có thể pickle bằng import path
(edge_gateway.ai_worker.ai_worker_process) khi tạo process con.
"""

import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime

import cv2
import numpy as np
import multiprocessing as mp

from . import config
from .logging_setup import log


def ai_worker_process(trigger_q: "mp.Queue", result_q: "mp.Queue"):
    """
    Chạy trong process riêng biệt (không share GIL với main process),
    nhờ đó vòng lặp MQTT ở main process không bao giờ bị nghẽn khi AI
    đang predict/capture ảnh. Xử lý tuần tự từng trigger lấy từ queue.
    """
    from ultralytics import YOLO

    try:
        model = YOLO(config.MODEL_PATH, task="segment")
        dummy = np.zeros((720, 1280, 3), dtype=np.uint8)
        model.predict(source=dummy, device=0, imgsz=640, verbose=False)
        log.info(f"[AI Worker] Model loaded & warmed up ({config.MODEL_PATH}).")
    except Exception as e:
        log.error(f"[AI Worker] Failed to load model: {e}")
        model = None

    while True:
        task = trigger_q.get()  # block tới khi có trigger
        if task is None:
            break

        node_id, trigger_value, severity, duration_sec, node_config, camera_config = (
            task
        )
        # node_config/camera_config luôn đã được validate not-None ở on_local_message
        # trước khi đẩy vào trigger_q, nên không cần fallback ở đây.
        camera_id = node_config.get("camera_id")
        local_image_path = f"temp_{camera_id}_{int(time.time())}.jpg"
        frame = None
        cam_type = camera_config.get("camera_type")
        event_id = str(uuid.uuid4())

        log.info(
            f"[AI Worker] Task IN <- node={node_id} severity={severity} value={trigger_value} "
            f"duration_sec={duration_sec} camera={camera_id} type={cam_type} event={event_id}"
        )

        try:
            if cam_type == "CSI":
                log.info(f"[AI Worker] Capturing CSI frame -> {local_image_path}")
                result = subprocess.run(
                    [
                        "gst-launch-1.0",
                        "nvarguscamerasrc",
                        "num-buffers=1",
                        "!",
                        "video/x-raw(memory:NVMM),width=1280,height=720,framerate=120/1",
                        "!",
                        "nvvidconv",
                        "!",
                        "jpegenc",
                        "!",
                        "filesink",
                        f"location={local_image_path}",
                    ],
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode == 0 and os.path.exists(local_image_path):
                    frame = cv2.imread(local_image_path)
                    log.info(f"[AI Worker] CSI capture OK -> {local_image_path}")
                else:
                    log.warning(
                        f"[AI Worker] CSI capture failed node={node_id} rc={result.returncode} "
                        f"stderr={result.stderr[:300] if result.stderr else None}"
                    )

            elif cam_type == "IP":
                stream_url = camera_config.get("stream_url")
                log.info(f"[AI Worker] Capturing IP frame from {stream_url}")
                if stream_url:
                    cap = cv2.VideoCapture(stream_url)
                    ret, frame = cap.read()
                    cap.release()
                    if ret:
                        cv2.imwrite(local_image_path, frame)
                        log.info(f"[AI Worker] IP capture OK -> {local_image_path}")
                    else:
                        log.warning(
                            f"[AI Worker] IP capture failed node={node_id} stream_url={stream_url}"
                        )
                else:
                    log.warning(
                        f"[AI Worker] camera_type IP nhưng thiếu stream_url, node={node_id}"
                    )

            if frame is None:
                log.warning(
                    f"[AI Worker] Capture failed for {camera_id}, using fallback image."
                )
                if os.path.exists(config.FALLBACK_IMAGE_PATH):
                    frame = cv2.imread(config.FALLBACK_IMAGE_PATH)
                    cv2.imwrite(local_image_path, frame)
                else:
                    # Không có ảnh để xử lý -> vẫn phải báo kết quả để giải phóng busy_nodes
                    log.error(
                        f"[AI Worker] Không có frame và không có fallback image, node={node_id} event={event_id}"
                    )
                    result_q.put(
                        {
                            "event_id": event_id,
                            "node_id": node_id,
                            "camera_id": camera_id,
                            "severity": severity,
                            "measured_val": trigger_value,
                            "duration_sec": duration_sec,
                            "crack_detected": False,
                            "confidence": 0.0,
                            "crack_size": 0.0,
                            "timestamp": datetime.utcnow().isoformat(),
                            "image_path": None,
                            "ai_failed": True,
                            "error": "no_frame_and_no_fallback",
                        }
                    )
                    log.info(
                        f"[AI Worker] Result OUT -> event={event_id} ai_failed=True error=no_frame_and_no_fallback"
                    )
                    continue

            crack_detected = False
            max_confidence = 0.0
            crack_size_estimation = 0.0

            if model:
                # Chạy predict trong thread riêng với timeout, tránh 1 lần predict
                # bị treo (VD lỗi driver GPU) làm nghẽn toàn bộ pipeline AI phía sau.
                log.info(
                    f"[AI Worker] Predict start node={node_id} timeout={config.PREDICT_TIMEOUT_SEC}s"
                )
                predict_started = time.time()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        model.predict,
                        source=frame,
                        device=0,
                        imgsz=640,
                        conf=0.25,
                        verbose=False,
                    )
                    try:
                        results = future.result(timeout=config.PREDICT_TIMEOUT_SEC)
                    except FutureTimeoutError:
                        log.warning(
                            f"[AI Worker] Predict timeout ({config.PREDICT_TIMEOUT_SEC}s) cho node {node_id}, bỏ qua trigger này."
                        )
                        result_q.put(
                            {
                                "event_id": event_id,
                                "node_id": node_id,
                                "camera_id": camera_id,
                                "severity": severity,
                                "measured_val": trigger_value,
                                "duration_sec": duration_sec,
                                "crack_detected": False,
                                "confidence": 0.0,
                                "crack_size": 0.0,
                                "timestamp": datetime.utcnow().isoformat(),
                                "image_path": local_image_path,
                                "ai_failed": True,
                                "error": "predict_timeout",
                            }
                        )
                        log.info(
                            f"[AI Worker] Result OUT -> event={event_id} ai_failed=True error=predict_timeout"
                        )
                        continue

                log.info(
                    f"[AI Worker] Predict done node={node_id} elapsed={time.time() - predict_started:.2f}s"
                )

                for res in results:
                    if res.masks is not None and len(res.masks) > 0:
                        crack_detected = True
                        max_confidence = float(max(res.boxes.conf.tolist()))
                        crack_size_estimation = float(res.masks.data.sum())
                        annotated = res.plot()
                        cv2.imwrite(local_image_path, annotated)
                        break

            log.info(
                f"[AI Worker] Result -> node={node_id} event={event_id} crack_detected={crack_detected} "
                f"confidence={max_confidence:.2f} crack_size={crack_size_estimation:.0f}"
            )

            result_q.put(
                {
                    "event_id": event_id,
                    "node_id": node_id,
                    "camera_id": camera_id,
                    "severity": severity,
                    "measured_val": trigger_value,
                    "duration_sec": duration_sec,
                    "crack_detected": crack_detected,
                    "confidence": max_confidence,
                    "crack_size": crack_size_estimation,
                    "timestamp": datetime.utcnow().isoformat(),
                    "image_path": local_image_path,
                    "ai_failed": False,
                }
            )
            log.info(
                f"[AI Worker] Result OUT -> event={event_id} ai_failed=False image_path={local_image_path}"
            )

        except Exception as e:
            log.error(f"[AI Worker] Error processing trigger for {node_id}: {e}")
            if os.path.exists(local_image_path):
                os.remove(local_image_path)
            # Luôn báo kết quả (dù lỗi) để consumer giải phóng busy_nodes,
            # nếu không node này bị kẹt "busy" vĩnh viễn.
            result_q.put(
                {
                    "event_id": event_id,
                    "node_id": node_id,
                    "camera_id": camera_id,
                    "severity": severity,
                    "measured_val": trigger_value,
                    "duration_sec": duration_sec,
                    "crack_detected": False,
                    "confidence": 0.0,
                    "crack_size": 0.0,
                    "timestamp": datetime.utcnow().isoformat(),
                    "image_path": None,
                    "ai_failed": True,
                    "error": str(e),
                }
            )
            log.info(
                f"[AI Worker] Result OUT -> event={event_id} ai_failed=True error={e}"
            )
