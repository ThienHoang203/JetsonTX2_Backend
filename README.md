# JetsonTX2 Edge Gateway

Edge Gateway chạy trên Jetson TX2, đặt tại trạm quan trắc đập. Nhiệm vụ:

1. Nhận dữ liệu cảm biến (độ rung...) từ node ESP32 qua **MQTT local** (broker Mosquitto chạy trên chính Jetson).
2. Đánh giá ngưỡng độ rung (`edge_gateway/vibration_evaluator.py`). Khi vượt ngưỡng ALERT/CRITICAL, kích hoạt **AI worker** (process riêng) để chụp ảnh (CSI hoặc IP camera) và chạy model YOLO segmentation phát hiện vết nứt.
3. Gửi kết quả (trạng thái ngưỡng, cảnh báo, ảnh bằng chứng) lên **MQTT cloud** / **Backend API** (máy chủ trung tâm, biến `MACHINE_A_IP`).

## Kiến trúc

```
ESP32 ──MQTT(local, 1883)──▶ EdgeGateway (main.py)
                                 │
                                 ├─ vibration_evaluator: đánh giá ngưỡng
                                 ├─ trigger_q ──▶ ai_worker_process (process riêng, YOLO)
                                 │                    └─ result_q ──▶ ai_result_consumer
                                 ├─ upload_queue ──▶ evidence_upload_worker ──HTTP──▶ Backend API
                                 └─ MQTT cloud ──▶ Backend (telemetry / status / anomaly)
```

| File                                  | Vai trò                                                                                                   |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `main.py`                             | Entrypoint DUY NHẤT. Set `mp.set_start_method("spawn")` đúng 1 lần trước khi tạo `mp.Process`/`mp.Queue`. |
| `edge_gateway/gateway.py`             | Điều phối MQTT local/cloud, watchdog, upload worker.                                                      |
| `edge_gateway/ai_worker.py`           | Chạy trong process con: load YOLO, chụp ảnh, predict.                                                     |
| `edge_gateway/vibration_evaluator.py` | Đánh giá ngưỡng rung (mật độ mẫu + thời lượng đợt rung).                                                  |
| `edge_gateway/config.py`              | Cấu hình tập trung + config mặc định (mock) khi chưa có Backend.                                          |
| `docker-compose.yml`                  | Chạy Mosquitto broker (`local-mqtt`) + service AI (`yolo-backend`).                                       |

## Yêu cầu hệ thống

- Jetson TX2 (JetPack), Docker + `nvidia-container-runtime`.
- Camera CSI (`nvarguscamerasrc`) và/hoặc camera IP (RTSP/HTTP stream).
- Backend + MQTT cloud broker đã chạy, truy cập được từ Jetson qua IP khai báo ở `MACHINE_A_IP`.

## 1. File cần chuẩn bị thủ công (KHÔNG có trong git)

Copy các file sau vào **thư mục gốc dự án** (cùng cấp `main.py`, `docker-compose.yml`) trước khi chạy:

| File                           | Vai trò                                                                 | Ghi chú                                                                                                                                                                        |
| ------------------------------ | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `train-0-3.pt`                 | Model YOLO segmentation phát hiện vết nứt (`config.MODEL_PATH`)         | Bị `.gitignore` chặn (`*.pt`). Lấy từ nơi lưu model của team rồi copy vào đây. Thiếu file này → AI worker log lỗi `Failed to load model`, mọi trigger trả về `ai_failed=True`. |
| `Cracking-Control-in-Dam.webp` | Ảnh fallback khi camera capture thất bại (`config.FALLBACK_IMAGE_PATH`) | Thiếu → khi capture lỗi, event AI trả `ai_failed=True, error=no_frame_and_no_fallback` thay vì có ảnh.                                                                         |
| `.env` (tuỳ chọn)              | Biến môi trường theo từng trạm                                          | Xem mục 2. Không bắt buộc — nếu không có, code dùng giá trị default trong `config.py`.                                                                                         |

## 2. Cấu hình `.env` — nạp `GATEWAY_ID` và IP Backend

```bash
cp .env.example .env
```

Sửa `.env`:

```ini
# ID gateway đã đăng ký với Backend, theo convention: GTW-{station_code}-{device_code}
GATEWAY_ID=GTW-ST01-TX2A

# IP máy chủ Backend / MQTT cloud broker mà Jetson này kết nối tới
MACHINE_A_IP=192.168.1.93

# MQTT broker local trên chính Jetson (thường không cần đổi)
LOCAL_MQTT_HOST=127.0.0.1
LOCAL_MQTT_PORT=1883
CLOUD_MQTT_PORT=1883

# Mức log: INFO (mặc định) hoặc DEBUG
EDGE_LOG_LEVEL=INFO
```

`edge_gateway/config.py` tự đọc file `.env` (không cần thư viện ngoài). `.env` đã nằm trong `.gitignore` — **không commit file này**.

## 3. Nạp ID cho Node (ESP32) — quan trọng

Gateway **không tự sinh ra node**. Mỗi node ESP32 tự publish theo topic MQTT cố định:

```
local/node/{node_id}/sensor/{sensor_type}
```

`node_id` do firmware ESP32 quyết định (không nằm trong repo này), theo convention `NOD-{station_code}-{device_code}` (VD: `NOD-ST01-ESP01`). Gateway chỉ xử lý được node nếu **đã có config khớp `node_id`** — nếu không, log sẽ báo:

```
Node {node_id} chưa có config, bỏ qua đánh giá ngưỡng.
```

Config của node (ngưỡng rung, camera gắn kèm...) đến từ 2 nguồn, ưu tiên Backend trước:

### a) Từ Backend API (production — khuyến nghị)

Khi gateway khởi động (`fetch_initial_config()`), nó gọi:

```
GET {BACKEND_API_URL}/gateway/{GATEWAY_ID}/config
```

trả về JSON dạng:

```json
{
  "nodes": {
    "NOD-ST01-ESP01": {
      "camera_id": "CAM-CSI-ST01-01",
      "warn_high": 2.5,
      "alert_high": 15.0,
      "critical_high": 25.0
    }
  },
  "cameras": {
    "CAM-CSI-ST01-01": { "camera_type": "CSI" }
  }
}
```

→ **Muốn thêm/sửa node: đăng ký/cập nhật trên Backend**, không sửa trực tiếp code Jetson. Backend cũng có thể đẩy config mới bất kỳ lúc nào qua topic MQTT `config/gateway/{GATEWAY_ID}/update` (gateway subscribe sẵn, tự cập nhật cache không cần restart).

Nếu request lỗi (Backend chưa chạy, sai IP, timeout...) → gateway tự fallback dùng **mock config** bên dưới.

### b) Mock config cục bộ (khi test không có Backend)

Sửa `DEFAULT_CONFIG` trong `edge_gateway/config.py`:

```python
DEFAULT_CONFIG = {
    "nodes": {
        "NOD-ST01-ESP01": {          # <-- node_id phải khớp topic ESP32 publish
            "camera_id": "CAM-CSI-ST01-01",
            "threshold": 15.0,        # fallback cho alert_high nếu không set
            "warn_high": 2.5,
            "alert_high": 15.0,
            "critical_high": 25.0,
        },
        # Thêm node khác tại đây, VD: "NOD-ST01-ESP02": {...}
    },
    "cameras": {
        "CAM-CSI-ST01-01": {"camera_type": "CSI"},
        # "CAM-IP-ST01-02": {"camera_type": "IP", "stream_url": "rtsp://..."},
    },
}
```

Mỗi node cần: `camera_id` (trỏ tới key trong `cameras`), `warn_high`/`alert_high`/`critical_high` (mm/s). Mỗi camera cần `camera_type` (`CSI` hoặc `IP`; `IP` cần thêm `stream_url`).

## 4. Chống lộ thông tin nhạy cảm khi commit

```bash
pip install pre-commit
pre-commit install
```

Hook chạy [gitleaks](https://github.com/gitleaks/gitleaks) quét secret trước mỗi `git commit`, chặn commit nếu phát hiện key/token/IP nhạy cảm.

## 5. Cách chạy

### Bước 1 — Chuẩn bị file thiếu

Copy `train-0-3.pt` và `Cracking-Control-in-Dam.webp` vào thư mục gốc (xem mục 1).

### Bước 2 — Cấu hình

`cp .env.example .env` rồi điền `GATEWAY_ID`, `MACHINE_A_IP` (mục 2). Nếu test không có Backend, chỉnh thêm `DEFAULT_CONFIG` trong `config.py` (mục 3b).

### Bước 3 — Chạy bằng Docker Compose (khuyến nghị)

```bash
docker compose up -d
```

Lệnh này khởi động:

- `local-mqtt`: broker Mosquitto (`mosquitto.conf`).
- `yolo-backend`: cài `paho-mqtt`, `httpx` rồi chạy `python3 main.py` (image sẵn `ultralytics`/YOLO, CUDA, OpenCV).

Xem log:

```bash
docker compose logs -f yolo-backend
```

### Bước 4 — Chạy trực tiếp (không Docker, debug trên Jetson)

Cài trước: `paho-mqtt`, `httpx`, `opencv-python`, `numpy`, `ultralytics`.

```bash
mosquitto -c mosquitto.conf &
EDGE_LOG_LEVEL=DEBUG python3 main.py
```

⚠️ Luôn chạy qua `main.py` — không import trực tiếp `edge_gateway.gateway`/`edge_gateway.ai_worker` ở nơi khác, để đảm bảo `mp.set_start_method("spawn")` được set đúng 1 lần trước khi tạo `Process`/`Queue` (bắt buộc để CUDA hoạt động đúng trong AI worker subprocess).

### Bước 5 — Dừng hệ thống

```bash
docker compose down
```

Hoặc `Ctrl+C` nếu chạy trực tiếp.

## Luồng dữ liệu MQTT

**Local (ESP32 → Gateway)**

- Subscribe: `local/node/+/sensor/+`

**Cloud (Gateway → Backend)**

- `telemetry/gateway/{GATEWAY_ID}/node/{node_id}/{sensor_type}` — relay dữ liệu thô.
- `status/gateway/{GATEWAY_ID}/node/{node_id}/vibration` — trạng thái ngưỡng theo thời gian thực.
- `events/gateway/{GATEWAY_ID}/anomaly` — sự kiện bất thường (kèm kết quả AI).
- Subscribe: `config/gateway/{GATEWAY_ID}/update` — nhận cấu hình node/camera cập nhật từ Backend.

**HTTP (Gateway → Backend API)**

- `GET {BACKEND_API_URL}/gateway/{GATEWAY_ID}/config` — lấy cấu hình node/camera ban đầu.
- `POST {BACKEND_API_URL}/evidence/upload` — upload ảnh bằng chứng kèm metadata sự kiện.

## Troubleshooting

| Triệu chứng                                        | Nguyên nhân thường gặp                                                                                          |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `Failed to load model`                             | Thiếu/sai `train-0-3.pt` ở thư mục gốc, sai định dạng YOLO segmentation.                                        |
| Event AI báo `no_frame_and_no_fallback`            | Camera capture lỗi **và** thiếu `Cracking-Control-in-Dam.webp`.                                                 |
| Gateway không kết nối MQTT local                   | `local-mqtt` (hoặc `mosquitto -c mosquitto.conf`) chưa chạy, sai cổng `1883`.                                   |
| `Node {id} chưa có config, bỏ qua đánh giá ngưỡng` | node_id ESP32 publish không khớp key nào trong `nodes` (Backend hoặc mock config) — xem mục 3.                  |
| Gateway dùng mock config dù đã có Backend          | Backend không phản hồi/`MACHINE_A_IP` sai — kiểm tra log `HTTP connection error` hoặc `Failed to fetch config`. |
