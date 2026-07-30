"""Giả lập thiết bị gửi dữ liệu ho qua socket cục bộ.

Windows dùng TCP 127.0.0.1:9000. Linux/Raspberry Pi dùng Unix Domain Socket
/tmp/cough_gw.sock, khớp với SocketServer của gateway.
"""

from __future__ import annotations

import json
import os
import random
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

HOST = os.environ.get("COUGH_GW_HOST", "127.0.0.1")
PORT = int(os.environ.get("COUGH_GW_PORT", "9000"))
SOCK_PATH = os.environ.get("COUGH_GW_SOCKET", "/tmp/cough_gw.sock")


def send_payload(payload: dict) -> None:
    """Encode and send one JSON-line payload through the local socket."""
    try:
        if sys.platform == "win32":
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            address = (HOST, PORT)
        else:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            address = SOCK_PATH

        with client:
            client.connect(address)
            message = json.dumps(payload) + "\n"
            client.sendall(message.encode("utf-8"))

        print(
            f"📤 Gửi: {payload.get('client_id')} | "
            f"Loại: {payload.get('cough_type')} | "
            f"Lúc: {payload.get('received_ts')}"
        )
    except Exception as exc:
        print(f"❌ Lỗi kết nối Socket: {exc}")


def simulate_hourly_data() -> None:
    print("=" * 60)
    print("🚀 BẮT ĐẦU BƠM DỮ LIỆU TRONG 24 GIỜ GẦN NHẤT...")
    print("=" * 60)

    fleet = [
        {"device_id": "DEV_001", "client_id": "Patient_01_NguyenVanA"},
        {"device_id": "DEV_002", "client_id": "Patient_02_TranThiB"},
        {"device_id": "DEV_003", "client_id": "Patient_03_LeVanC"},
    ]

    cough_types = ["wet", "dry", "unknown"]

    print("\n[Bước 1] Đăng ký trạng thái kết nối thiết bị...")
    for dev in fleet:
        send_payload({
            "type": "status",
            "device_id": dev["device_id"],
            "client_id": dev["client_id"],
            "connected": True,
        })
        time.sleep(0.2)

    print("\n[Bước 2] Tạo sự kiện ho rải đều trong 24 giờ gần nhất...")
    now = datetime.now(timezone.utc)
    counter = 1

    for dev in fleet:
        for _ in range(50):
            event_time = now - timedelta(seconds=random.randint(0, 24 * 60 * 60 - 1))
            ts_str = event_time.replace(microsecond=0).isoformat()
            cough_type = random.choices(
                cough_types,
                weights=[0.5, 0.45, 0.05],
            )[0]

            send_payload({
                "type": "cough",
                "device_id": dev["device_id"],
                "client_id": dev["client_id"],
                "cough_type": cough_type,
                "counter": counter,
                "event_ts": ts_str,
                "received_ts": ts_str,
            })
            counter += 1
            time.sleep(0.01)

    print("=" * 60)
    print("🎉 HOÀN TẤT BƠM DỮ LIỆU!")
    print("=" * 60)


if __name__ == "__main__":
    simulate_hourly_data()
