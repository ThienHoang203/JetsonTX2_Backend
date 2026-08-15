"""
Bộ kiểm tra vượt ngưỡng độ rung (PPV mm/s), kết hợp 2 tiêu chí độc lập để
quyết định breach ở mức ALERT:

- MẬT ĐỘ: đủ N lần vượt alert_high trong cửa sổ trượt window_size_sec giây
  (giữ nguyên cơ chế cũ, tốt khi sensor gửi dồn dập).
- THỜI LƯỢNG: một "đợt rung" (episode) vượt alert_high kéo dài liên tục đủ
  alert_min_duration_sec giây, kể cả khi sensor gửi thưa (VD 1 lần / 3-4s)
  nên không bao giờ gom đủ N mẫu trong window_size_sec.

Đợt rung được "mở/gia hạn" dựa trên alert_high (không phải warn_high) - chỉ
mẫu thật sự đáng báo động mới giữ đồng hồ chạy. episode_reset_gap_sec cho
phép một vài mẫu dao động nhỏ tụt xuống dưới alert_high (kể cả về tới vùng
WARNING/NORMAL) mà không làm mất đợt rung ngay, miễn khoảng trống đó chưa
vượt quá gap này. Nhờ vậy rung nền bình thường (chưa từng chạm alert_high)
sẽ không bao giờ mở đợt rung, duration_sec giữ nguyên 0 - không bị đếm vô
hạn chỉ vì lởn vởn quanh warn_high.

Mọi ngưỡng (warn_high/alert_high/critical_high/alert_min_count/
alert_min_duration_sec/episode_reset_gap_sec) đều nhận từ config backend
theo từng node, evaluator chỉ giữ giá trị mặc định khi node chưa override.
"""
import threading

from .logging_setup import log


class VibrationWindowEvaluator:
    def __init__(
        self,
        window_size_sec: float = 10.0,
        alert_min_count: int = 4,
        alert_min_duration_sec: float = 6.0,
        episode_reset_gap_sec: float = 3.0,
    ):
        self.window_size_sec = window_size_sec
        self.alert_min_count = alert_min_count
        self.alert_min_duration_sec = alert_min_duration_sec
        self.episode_reset_gap_sec = episode_reset_gap_sec

        # Đếm mật độ mẫu >= alert_high trong cửa sổ trượt: { node_id: [timestamp, ...] }
        self.alert_window_map = {}
        # Đợt rung (>= alert_high) đang diễn ra: { node_id: {"start", "last_seen", "peak_ppv"} }
        self.episodes = {}
        self.lock = threading.Lock()

    def _cleanup_empty(self, node_id: str, timestamps: list):
        if timestamps:
            self.alert_window_map[node_id] = timestamps
        else:
            self.alert_window_map.pop(node_id, None)

    def _update_episode(
        self, node_id: str, ppv: float, timestamp_sec: float, episode_floor: float, reset_gap: float
    ) -> float:
        """Mở/gia hạn/đóng đợt rung cho node, trả về thời lượng đợt rung hiện tại (giây).

        episode_floor = alert_high: chỉ mẫu thật sự đáng báo động mới mở/gia hạn
        đợt rung, tránh rung nền (>= warn_high nhưng chưa từng chạm alert_high)
        khiến duration_sec đếm vô hạn không bao giờ tắt.
        """
        ep = self.episodes.get(node_id)

        if ppv >= episode_floor:
            if ep is None or (timestamp_sec - ep["last_seen"]) > reset_gap:
                # Chưa có đợt nào đang mở, hoặc đợt cũ đã nguội quá lâu -> mở đợt mới
                ep = {"start": timestamp_sec, "last_seen": timestamp_sec, "peak_ppv": ppv}
            else:
                ep["last_seen"] = timestamp_sec
                ep["peak_ppv"] = max(ep["peak_ppv"], ppv)
            self.episodes[node_id] = ep
            return timestamp_sec - ep["start"]

        # Mẫu này dưới episode_floor: đợt vẫn còn "sống" nếu chưa quá reset_gap
        # kể từ mẫu đáng báo động gần nhất (cho phép tụt tạm về WARNING/NORMAL).
        if ep is not None:
            if (timestamp_sec - ep["last_seen"]) <= reset_gap:
                return timestamp_sec - ep["start"]
            self.episodes.pop(node_id, None)
        return 0.0

    def evaluate(
        self,
        node_id: str,
        ppv: float,
        timestamp_sec: float,
        warn_high: float = 2.5,
        alert_high: float = 15.0,
        critical_high: float = 25.0,
        alert_min_count: int = None,
        alert_min_duration_sec: float = None,
        episode_reset_gap_sec: float = None,
    ):
        """
        Đánh giá giá trị độ rung PPV (mm/s):
        - CRITICAL (>= critical_high): nguy hiểm tức thời, breach ngay bất kể mật độ/thời lượng.
        - ALERT (>= alert_high): breach nếu đủ mật độ (>= alert_min_count lần/window_size_sec)
          HOẶC đợt rung đã kéo dài >= alert_min_duration_sec giây — cái nào tới trước.
        - WARNING (>= warn_high): mức theo dõi, chưa breach.
        - NORMAL (< warn_high): an toàn.

        Trả về: (breach: bool, severity: str, duration_sec: float) — duration_sec luôn là
        thời lượng thực của đợt rung đang diễn ra (hoặc mới kết thúc trong grace gap),
        áp dụng cho mọi severity, không chỉ riêng ALERT.
        """
        min_count = self.alert_min_count if alert_min_count is None else alert_min_count
        min_duration = (
            self.alert_min_duration_sec if alert_min_duration_sec is None else alert_min_duration_sec
        )
        reset_gap = self.episode_reset_gap_sec if episode_reset_gap_sec is None else episode_reset_gap_sec

        log.info(
            f"[Evaluate] IN <- node={node_id} ppv={ppv} warn={warn_high} alert={alert_high} "
            f"critical={critical_high} min_count={min_count} min_duration={min_duration}s "
            f"reset_gap={reset_gap}s"
        )

        with self.lock:
            # Luôn cập nhật đợt rung trước (neo theo alert_high, không phải warn_high),
            # để duration_sec phản ánh đúng thời lượng của rung ĐÁNG BÁO ĐỘNG thực tế,
            # không đếm vô hạn chỉ vì rung nền lởn vởn quanh warn_high.
            episode_duration = self._update_episode(node_id, ppv, timestamp_sec, alert_high, reset_gap)

            # 1. CRITICAL - nguy hiểm tức thời
            if ppv >= critical_high:
                log.warning(
                    f"[Threshold] Node {node_id} VƯỢT NGƯỠNG NGUY CẤP: {ppv} mm/s >= {critical_high} mm/s "
                    f"(đợt rung đã kéo dài {episode_duration:.1f}s)"
                )
                log.info(f"[Evaluate] OUT -> node={node_id} breach=True severity=CRITICAL duration_sec={episode_duration:.1f}")
                return True, "CRITICAL", episode_duration

            # Cửa sổ trượt đếm mật độ (giữ nguyên cơ chế cũ cho tiêu chí mật độ)
            if node_id not in self.alert_window_map:
                self.alert_window_map[node_id] = []
            timestamps = self.alert_window_map[node_id]
            cutoff = timestamp_sec - self.window_size_sec
            timestamps = [t for t in timestamps if t >= cutoff]
            self._cleanup_empty(node_id, timestamps)

            # 2. ALERT - vượt theo MẬT ĐỘ hoặc THỜI LƯỢNG
            if ppv >= alert_high:
                timestamps.append(timestamp_sec)
                self.alert_window_map[node_id] = timestamps

                by_count = len(timestamps) >= min_count
                by_duration = episode_duration >= min_duration

                if by_count or by_duration:
                    reason = "mật độ" if by_count else "thời lượng"
                    log.warning(
                        f"[Threshold] Node {node_id} VƯỢT NGƯỠNG BÁO ĐỘNG LIÊN TIẾP ({reason}): "
                        f"{ppv} mm/s, {len(timestamps)} lần/{self.window_size_sec:.0f}s, "
                        f"đợt rung {episode_duration:.1f}s"
                    )
                    log.info(f"[Evaluate] OUT -> node={node_id} breach=True severity=ALERT duration_sec={episode_duration:.1f}")
                    return True, "ALERT", episode_duration

                log.info(
                    f"[Threshold] Node {node_id} chớm vượt ngưỡng ALERT ({ppv} mm/s): "
                    f"{len(timestamps)}/{min_count} lần, {episode_duration:.1f}/{min_duration:.1f}s đợt rung."
                )
                log.info(f"[Evaluate] OUT -> node={node_id} breach=False severity=WARNING duration_sec={episode_duration:.1f}")
                return False, "WARNING", episode_duration

            # 3. WARNING - theo dõi, vẫn báo cáo đúng thời lượng đợt rung đang diễn ra
            if ppv >= warn_high:
                log.info(f"[Evaluate] OUT -> node={node_id} breach=False severity=WARNING duration_sec={episode_duration:.1f}")
                return False, "WARNING", episode_duration

            # 4. NORMAL - duration_sec chỉ còn > 0 nếu vẫn trong grace gap của đợt rung vừa kết thúc
            log.info(f"[Evaluate] OUT -> node={node_id} breach=False severity=NORMAL duration_sec={episode_duration:.1f}")
            return False, "NORMAL", episode_duration
