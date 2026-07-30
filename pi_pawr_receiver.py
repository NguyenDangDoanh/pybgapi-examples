#!/usr/bin/env python3
"""
Pi + BGM220 (NCP): nhan du lieu cam bien tu xG26 qua PAwR -> day len ThingsBoard.

Luong:  xG26 (PAwR advertiser) --PAwR--> BGM220/Pi (sync) --HTTP--> ThingsBoard

Cai dat:  pip install pybgapi requests
Chay:     python3 pi_pawr_receiver.py
"""
import struct
import time
import requests
import bgapi

# ---- NCP / PAwR ----
XAPI   = "api/sl_bt.xapi"          # file dac ta API (di kem Bluetooth SDK)
PORT   = "/dev/ttyACM0"        # cong serial BGM220 NCP
BAUD   = 115200
TARGET = "54:dc:e9:32:21:ac"   # dia chi xG26 (in ra luc boot, chu thuong)

PAYLOAD_FMT  = "<iIhhhhhhh"    # 22 byte
PAYLOAD_SIZE = struct.calcsize(PAYLOAD_FMT)

# ---- ThingsBoard ----
TB_HOST   = "vinhiot.duckdns.org"
TB_PORT   = 9090                       # cong UI/HTTP cua TB
TB_TOKEN  = "eQgJrXUYQi9jwL5sWJ2K"     # access token device BreathSensor_PAwR
TB_URL    = f"http://{TB_HOST}:{TB_PORT}/api/v1/{TB_TOKEN}/telemetry"
TB_PERIOD = 1.0                        # gioi han day len TB: 1 lan / giay

_last_post = 0.0

def parse(data: bytes) -> dict:
    t, h, ax, ay, az, gx, gy, gz, s10 = struct.unpack(PAYLOAD_FMT, data[:PAYLOAD_SIZE])
    return {
        "temperature": round(t / 1000.0, 2),   # degC
        "humidity":    round(h / 1000.0, 2),    # %RH
        "acc_x": ax, "acc_y": ay, "acc_z": az,  # mg
        "gyro_x": gx / 10.0, "gyro_y": gy / 10.0, "gyro_z": gz / 10.0,  # do/s
        "sound_db": s10 / 10.0,                 # dB
    }

def send_thingsboard(tele: dict):
    """POST telemetry len ThingsBoard (gioi han 1 Hz de khong spam)."""
    global _last_post
    now = time.time()
    if now - _last_post < TB_PERIOD:
        return
    _last_post = now
    try:
        r = requests.post(TB_URL, json=tele, timeout=5)
        if r.status_code != 200:
            print("TB HTTP", r.status_code, r.text)
    except Exception as e:
        print("TB error:", e)

def main():
    lib = bgapi.BGLib(bgapi.SerialConnector(PORT, baudrate=BAUD), XAPI)
    lib.open()
    lib.bt.system.reboot()

    synced = False

    for evt in lib.gen_events(timeout=None, max_time=None):
        if evt == "bt_evt_system_boot":
            print("NCP ready, scanning for", TARGET)
            # lib.bt.sync_scanner.set_sync_parameters(0, 1000, 1)   # skip, timeout(x10ms), reporting
            lib.bt.scanner.start(1, 2)                            # PHY 1M, observation

        elif evt == "bt_evt_scanner_extended_advertisement_report":
            if (not synced
                    and str(evt.address).lower() == TARGET
                    and getattr(evt, "periodic_interval", 0) > 0):
                print("Found train, opening sync (sid=%d)" % evt.adv_sid)
                lib.bt.sync_scanner.open(evt.address, evt.address_type, evt.adv_sid)
                synced = True
                lib.bt.scanner.stop()

        elif evt == "bt_evt_pawr_sync_opened":
            print("PAwR sync opened")
            lib.bt.pawr_sync.set_sync_subevents(evt.sync, b"\x00")   # nghe subevent 0

        elif evt == "bt_evt_pawr_sync_subevent_report":
            data = evt.data
            if len(data) >= PAYLOAD_SIZE:
                tele = parse(data)
                print(tele)
                send_thingsboard(tele)        # <-- day len ThingsBoard

if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Cach khac: dung MQTT thay cho HTTP (pip install paho-mqtt)
#   import paho.mqtt.client as mqtt, json
#   m = mqtt.Client()
#   m.username_pw_set(TB_TOKEN)            # username = access token, khong password
#   m.connect(TB_HOST, 1883, 60)          # MQTT port mac dinh TB = 1883
#   m.loop_start()
#   m.publish("v1/devices/me/telemetry", json.dumps(tele))
# ---------------------------------------------------------------------------
