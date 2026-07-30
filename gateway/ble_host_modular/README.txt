BreathSense modular BLE host v2

Current behavior
----------------
- Single xG26 peripheral connection.
- Discovers one BreathSense service.
- Discovers and subscribes to both:
  1. Cough characteristic
     UUID b5e00002-7a4b-4c6d-9e10-112233445566
     Payload <BBIH, 8 bytes.
  2. Environment characteristic
     UUID b5e00003-7a4b-4c6d-9e10-112233445566
     Payload <hH, 4 bytes.
- Distinguishes notifications by characteristic handle.
- Prints JSON to stdout.
- Optionally forwards JSON Lines to a Unix socket backend.

Files
-----
main.py
config.py
constants.py
models.py
ble_central.py
advertisement.py
payload_parser.py
backend_client.py
utils.py

Run
---
python main.py SERIAL_PORT --xapi PATH_TO_sl_bt.xapi -l INFO

Important
---------
This version requires both characteristics to exist and support Notify.
If the environment characteristic is not yet present in firmware, the host
will disconnect after discovery and log which characteristic is missing.
