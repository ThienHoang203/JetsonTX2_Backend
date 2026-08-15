"""
Logger dùng chung cho toàn bộ package edge_gateway.
Import: from edge_gateway.logging_setup import log

Đổi mức log lúc debug (không cần sửa code) bằng biến môi trường, ví dụ:
    EDGE_LOG_LEVEL=DEBUG python3 main.py
DEBUG sẽ bật thêm log chi tiết ở từng bước xử lý message (input nhận được,
kết quả evaluate, payload publish...) mà mức INFO mặc định không in ra để
tránh làm ngợp log khi chạy production.
"""
import logging
import os
import warnings

warnings.filterwarnings("ignore", message=".*_floordiv_ is deprecated.*")

_LEVEL_NAME = os.environ.get("EDGE_LOG_LEVEL", "INFO").upper()
_LEVEL = getattr(logging, _LEVEL_NAME, logging.INFO)

# %(processName)s/%(threadName)s giúp phân biệt log tới từ đâu: main thread (MQTT
# loop), evidence_upload_worker, ai_result_consumer, watchdog_loop, hay từ
# ai_worker_process (chạy ở process con riêng) - rất cần khi debug hệ thống
# nhiều thread/process chạy song song như gateway này.
logging.basicConfig(
    level=_LEVEL,
    format="%(asctime)s [%(levelname)s] [%(processName)s:%(threadName)s] %(message)s",
)
log = logging.getLogger("edge")
