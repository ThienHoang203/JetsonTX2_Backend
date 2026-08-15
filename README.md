# JetsonTX2 Edge Gateway

Edge Gateway chạy trên Jetson TX2, đặt tại trạm quan trắc đập. Nhiệm vụ:

1. Nhận dữ liệu cảm biến (độ rung...) từ node ESP32 qua **MQTT local** (broker Mosquitto chạy trên chính Jetson).
2. Đánh giá ngưỡng độ rung (`edge_gateway/vibration_evaluator.py`). Khi vượt ngưỡng ALERT/CRITICAL, kích hoạt **AI worker** (process riêng) để chụp ảnh (CSI hoặc IP camera) và chạy model YOLO segmentation phát hiện vết nứt.
3. Gửi kết quả (trạng thái ngưỡng, cảnh báo, ảnh bằng chứng) lên **MQTT cloud** / **Backend API** (máy chủ trung tâm, biến `MACHINE_A_IP` trong `edge_gateway/config.py`).

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

- `main.py`: entrypoint DUY NHẤT. Bắt buộc chạy qua file này (set `mp.set_start_method("spawn")` đúng 1 lần trước khi tạo `mp.Process`/`mp.Queue`).
- `edge_gateway/gateway.py`: điều phối MQTT local/cloud, watchdog, upload worker.
- `edge_gateway/ai_worker.py`: chạy trong process con, load YOLO, chụp ảnh, predict.
- `edge_gateway/vibration_evaluator.py`: logic đánh giá ngưỡng rung (mật độ + thời lượng).
- `edge_gateway/config.py`: cấu hình tập trung (IP, MQTT, ngưỡng, đường dẫn model...).
- `docker-compose.yml`: chạy Mosquitto broker (`local-mqtt`) + service AI (`yolo-backend`).

## Yêu cầu hệ thống

- Jetson TX2 (JetPack), Docker + `nvidia-container-runtime`.
- Camera CSI gắn vào Jetson (dùng `nvarguscamerasrc`) và/hoặc camera IP (RTSP/HTTP stream).
- Máy chủ Backend + MQTT cloud broker đã chạy sẵn, có thể truy cập từ Jetson qua IP khai báo ở `MACHINE_A_IP`.

## File cần thiết KHÔNG có trong git

Các file sau bị loại khỏi git (qua `.gitignore` hoặc chưa từng được thêm) — phải tự chuẩn bị/copy thủ công vào thư mục gốc dự án (cùng cấp `main.py`) trước khi chạy, nếu không hệ thống sẽ lỗi hoặc chạy sai:

| File | Vai trò | Ghi chú |
|---|---|---|
| `train-0-3.pt` | Model YOLO segmentation phát hiện vết nứt (`config.MODEL_PATH`) | Bị `.gitignore` chặn (`*.pt`). Lấy từ nơi lưu trữ model của team (Drive/NAS/…) rồi copy vào đây. |
| `Cracking-Control-in-Dam.webp` | Ảnh fallback dùng khi camera capture thất bại (`config.FALLBACK_IMAGE_PATH`) | Không nằm trong `.gitignore` nhưng cũng chưa được commit — cần tự thêm, nếu thiếu thì khi capture lỗi, event AI sẽ báo `ai_failed=True, error=no_frame_and_no_fallback` thay vì có ảnh fallback. |
| `.env` (tuỳ chọn) | Biến môi trường | Hiện code chỉ đọc `EDGE_LOG_LEVEL` (mặc định `INFO`, có thể set `DEBUG`). Không bắt buộc phải có file `.env`; có thể export trực tiếp trong shell hoặc `environment:` của `docker-compose.yml`. |

> `edge_gateway/config.py` đọc `GATEWAY_ID`, `MACHINE_A_IP`, `LOCAL_MQTT_HOST/PORT`, `CLOUD_MQTT_PORT` từ biến môi trường / file `.env` (tự nạp, không cần thư viện ngoài), có default để chạy thử cục bộ. Copy `.env.example` → `.env` và điền giá trị thật cho trạm của bạn:
> ```bash
> cp .env.example .env
> ```
> `.env` đã nằm trong `.gitignore` — không commit file này.

## Chống lộ thông tin nhạy cảm khi commit

Repo có cấu hình [pre-commit](https://pre-commit.com/) chạy [gitleaks](https://github.com/gitleaks/gitleaks) để quét secret trước mỗi commit. Cài 1 lần:
```bash
pip install pre-commit
pre-commit install
```
Sau đó mỗi lần `git commit`, hook sẽ tự quét diff và chặn commit nếu phát hiện secret (key, token, IP/credential dạng nhạy cảm theo rule mặc định của gitleaks).

## Cách chạy

### 1. Chuẩn bị file thiếu
Copy `train-0-3.pt` và `Cracking-Control-in-Dam.webp` vào thư mục gốc dự án (cùng cấp `main.py`, `docker-compose.yml`).

### 2. Kiểm tra cấu hình
Mở `edge_gateway/config.py`, xác nhận:
- `MACHINE_A_IP` trỏ đúng IP máy Backend.
- `GATEWAY_ID` là ID gateway đã đăng ký với Backend.

### 3. Chạy bằng Docker Compose (khuyến nghị)
```bash
docker compose up -d
```
Lệnh này sẽ:
- Khởi động broker `local-mqtt` (Eclipse Mosquitto) dùng `mosquitto.conf`.
- Khởi động `yolo-backend`: cài `paho-mqtt`, `httpx` rồi chạy `python3 main.py` (image đã có sẵn `ultralytics`/YOLO, CUDA, OpenCV).

Xem log:
```bash
docker compose logs -f yolo-backend
```

### 4. Chạy trực tiếp (không Docker, khi debug trên Jetson)
Yêu cầu đã cài: `paho-mqtt`, `httpx`, `opencv-python`, `numpy`, `ultralytics`, và Mosquitto broker chạy sẵn ở `127.0.0.1:1883` (dùng `mosquitto.conf`).

```bash
mosquitto -c mosquitto.conf &
EDGE_LOG_LEVEL=DEBUG python3 main.py
```

⚠️ Không import/chạy trực tiếp `edge_gateway.gateway` hay `edge_gateway.ai_worker` ở nơi khác — luôn chạy qua `main.py` để đảm bảo `multiprocessing.set_start_method("spawn")` được set đúng 1 lần trước khi tạo `Process`/`Queue` (bắt buộc để CUDA hoạt động đúng trong AI worker subprocess).

### 5. Dừng hệ thống
```bash
docker compose down
```
Hoặc `Ctrl+C` nếu chạy trực tiếp (gateway sẽ disconnect MQTT và đẩy sentinel dừng AI worker trước khi thoát).

## Luồng dữ liệu MQTT

**Local (ESP32 → Gateway)**
- Subscribe: `local/node/+/sensor/+`

**Cloud (Gateway → Backend)**
- `telemetry/gateway/{GATEWAY_ID}/node/{node_id}/{sensor_type}` — relay dữ liệu thô.
- `status/gateway/{GATEWAY_ID}/node/{node_id}/vibration` — trạng thái ngưỡng theo thời gian thực.
- `events/gateway/{GATEWAY_ID}/anomaly` — sự kiện bất thường (kèm kết quả AI).
- Subscribe: `config/gateway/{GATEWAY_ID}/update` — nhận cấu hình node/camera cập nhật từ Backend.

**HTTP (Gateway → Backend API)**
- `GET {BACKEND_API_URL}/gateway/{GATEWAY_ID}/config` — lấy cấu hình ban đầu (fallback về mock config nếu lỗi).
- `POST {BACKEND_API_URL}/evidence/upload` — upload ảnh bằng chứng kèm metadata sự kiện.

## Troubleshooting nhanh

- **AI worker không load được model**: kiểm tra `train-0-3.pt` có tồn tại đúng thư mục gốc, đúng quyền đọc, đúng định dạng segmentation YOLO.
- **Không có ảnh khi capture lỗi**: kiểm tra `Cracking-Control-in-Dam.webp` đã có ở thư mục gốc.
- **Gateway không kết nối MQTT local**: đảm bảo container/service `local-mqtt` (hoặc `mosquitto -c mosquitto.conf`) đã chạy trước, đúng cổng `1883`.
- **Không thấy config node**: kiểm tra Backend đã trả đúng dữ liệu ở endpoint `/gateway/{GATEWAY_ID}/config`, hoặc gateway đang chạy tạm với mock config (`load_mock_config`).
