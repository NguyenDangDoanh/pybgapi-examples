BreathSense BLE host (multi-node)
================================

This host uses Raspberry Pi + BGM220 NCP as a BLE Central. It scans for xG26
peripherals, keeps one independent GATT discovery state per connection handle,
subscribes to Cough Event and Environment Data notifications, and forwards
JSON Lines to the local gateway socket.

Example:

PORT="/dev/serial/by-id/usb-Silicon_Labs_J-Link_OB_000440210672-if00"
python gateway/ble_host_modular/main.py "$PORT" \
  --xapi api/sl_bt.xapi \
  --name-prefix MyDevice \
  --max-connections 2 \
  --service-uuid b5e00001-7a4b-4c6d-9e10-112233445566 \
  --cough-uuid b5e00002-7a4b-4c6d-9e10-112233445566 \
  --environment-uuid b5e00003-7a4b-4c6d-9e10-112233445566 \
  --backend-socket /tmp/cough_gw.sock \
  -l INFO

Important:
- BGM220 NCP firmware must be configured for at least --max-connections links.
- An exact --address intentionally limits the host to one node.
- device.address is the stable device_id; connection handles are temporary.
- event_ts uses the node Unix timestamp when plausible. Current firmware sends
  zero, so the gateway receive time is used and timestamp_source explains why.
- Backend delivery is buffered and bounded; SQLite message_id deduplication
  prevents retries from creating duplicate rows.
- Connection-open and GATT procedure timeouts prevent one bad node from blocking the fleet.
