"""
Entrypoint DUY NHẤT của Edge Gateway. Chạy: python3 main.py

Không import trực tiếp edge_gateway.gateway hay edge_gateway.ai_worker ở nơi
khác để khởi động service - luôn đi qua file này để đảm bảo
mp.set_start_method("spawn") được set đúng 1 lần, trước khi bất kỳ
mp.Process/mp.Queue nào được tạo.
"""
import multiprocessing as mp

from edge_gateway.gateway import EdgeGateway


def main():
    # Bắt buộc "spawn" để CUDA hoạt động đúng trong subprocess AI worker.
    # PHẢI gọi trong if __name__ == "__main__", chỉ 1 lần, trước khi tạo
    # bất kỳ mp.Process/mp.Queue nào.
    mp.set_start_method("spawn", force=True)
    gateway = EdgeGateway()
    gateway.run()


if __name__ == "__main__":
    main()
