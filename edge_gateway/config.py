"""
Cấu hình tập trung cho Edge Gateway.
Mọi module khác import từ đây (import config; config.GATEWAY_ID) để
tránh circular import và để đổi cấu hình 1 chỗ duy nhất.

Các giá trị gắn với hạ tầng thực tế (IP, ID gateway...) đọc từ biến môi
trường (file .env, KHÔNG commit - xem .env.example) để tránh lộ thông tin
nội bộ khi push code lên git. Giá trị default chỉ để chạy thử cục bộ.
"""
import os

# Nạp file .env (nếu có) vào os.environ trước khi đọc config bên dưới.
# Không dùng thư viện ngoài (python-dotenv) để tránh thêm dependency chỉ cho
# việc parse KEY=VALUE đơn giản. .env không được commit (xem .gitignore).
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _val = _line.split("=", 1)
            os.environ.setdefault(_key.strip(), _val.strip())

# ================= GATEWAY / NETWORK =================
GATEWAY_ID = os.environ.get("GATEWAY_ID", "GTW-ST01-TX2A")
MACHINE_A_IP = os.environ.get("MACHINE_A_IP", "127.0.0.1")
BACKEND_API_URL = f"http://{MACHINE_A_IP}:3001/api"

LOCAL_MQTT_HOST = os.environ.get("LOCAL_MQTT_HOST", "127.0.0.1")
LOCAL_MQTT_PORT = int(os.environ.get("LOCAL_MQTT_PORT", "1883"))
CLOUD_MQTT_HOST = MACHINE_A_IP
CLOUD_MQTT_PORT = int(os.environ.get("CLOUD_MQTT_PORT", "1883"))

# ================= AI MODEL =================
MODEL_PATH = "train-0-3.pt"
FALLBACK_IMAGE_PATH = "Cracking-Control-in-Dam.webp"

# ================= TIMEOUT / QUEUE LIMITS =================
PREDICT_TIMEOUT_SEC = 15.0  # timeout cho 1 lần model.predict, tránh treo cả pipeline AI
BUSY_NODE_TIMEOUT_SEC = 30.0  # watchdog: force-release busy_nodes nếu quá lâu không có kết quả
UPLOAD_QUEUE_MAXSIZE = 20  # giới hạn hàng đợi upload, tránh phình RAM/disk khi backend down

# ================= VIBRATION SLIDING WINDOW =================
VIBRATION_WINDOW_SIZE_SEC = 10.0
VIBRATION_ALERT_MIN_COUNT = 4
# Breach ALERT nếu đợt rung liên tục vượt alert_high đủ lâu, kể cả khi sensor
# gửi thưa nên không kịp gom đủ VIBRATION_ALERT_MIN_COUNT mẫu trong window.
VIBRATION_ALERT_MIN_DURATION_SEC = 6.0
# Khoảng lặng tối đa giữa 2 mẫu "elevated" (>= warn_high) để vẫn coi là cùng
# 1 đợt rung - tránh dao động nhỏ làm reset nhầm đồng hồ đo thời lượng.
VIBRATION_EPISODE_RESET_GAP_SEC = 3.0

# ================= DEFAULT / MOCK CONFIG =================
DEFAULT_CONFIG = {
    "nodes": {
        "NOD-ST01-ESP01": {
            "camera_id": "CAM-CSI-ST01-01",
            "threshold": 15.0,
            "warn_high": 2.5,
            "alert_high": 15.0,
            "critical_high": 25.0,
        }
    },
    "cameras": {
        "CAM-CSI-ST01-01": {"camera_type": "CSI"}
    },
}
MOCK_CONFIG = DEFAULT_CONFIG

