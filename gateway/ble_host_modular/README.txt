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
  --time-uuid b5e00004-7a4b-4c6d-9e10-112233445566 \
  --backend-socket /tmp/cough_gw.sock \
  -l INFO

Important:
- BGM220 NCP firmware must be configured for at least --max-connections links.
- An exact --address intentionally limits the host to one node.
- device.address is the stable device_id; connection handles are temporary.
- The 8-byte Cough Event payload remains <BBIH>. event_ts > 0 uses node Unix
  time; event_ts == 0 falls back to Pi receive time for legacy firmware.
- Cough flags decode timestamp_valid (bit 0), stage2_valid (bit 1), prolonged
  (bit 2), and estimated duration_s (bits 3-7, maximum 31 seconds).
- UNKNOWN remains a valid event. A prolonged bout is a monitoring indication
  requiring observation, not a medical diagnosis.
- The optional writable Time characteristic receives little-endian uint32 Unix
  time after notifications are enabled and again at each UTC midnight. Its
  absence is supported for backward compatibility.
- If the BGM220 reader thread dies or the serial adapter is unplugged, all
  running nodes are reported disconnected and a fresh host retries the same
  serial path every 2 seconds until the NCP returns.
- Each connection owns its discovery/time-sync state. Time writes are
  asynchronous, and failure on one node does not stop the others.
- Firmware must keep a monotonic clock running across BLE disconnects and store
  flags, cough_type, captured event_ts, and uint16 event_counter for FIFO replay.
- Backend delivery is buffered and bounded; SQLite message_id deduplication
  prevents retries from creating duplicate rows.
- Connection-open and GATT procedure timeouts prevent one bad node from blocking the fleet.
