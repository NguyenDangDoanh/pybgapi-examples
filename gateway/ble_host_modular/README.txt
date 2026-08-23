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

BGM220 hot-unplug / hot-plug recovery
--------------------------------------

The supervisor owns exactly one BleCentral instance at a time. Reader-thread
death, raw serial/connector failures, BGAPI no-response/send-timeout/wrong-
response errors, and NCP boot timeout are normalized to NcpTransportLost. The
old BGLib/SerialConnector is closed best effort, the supervisor waits 2 seconds,
then constructs a completely fresh session. A CommandFailedError is different:
it means the NCP returned a valid response with a Bluetooth status code, so the
current command/node is handled without blindly rebuilding the NCP.

Each successful serial open logs the requested path, resolved tty, baud rate,
RTS/CTS mode, and session_id. With -l DEBUG, scanner candidates rejected for an
invalid address, missing name, wrong prefix, exact-address mismatch, retry
backoff, or an existing connection are rate-limited and logged. The expected
recovery milestones are:

1. Opened NCP serial port
2. BGM220 Bluetooth stack booted
3. Scanning for xG26
4. Found xG26
5. Connected to xG26
6. Notifications enabled and optional Time synchronization
7. connected status followed by environment_data/cough_event

The xG26 firmware is a separate part of this recovery contract. After BLE
supervision timeout it must handle sl_bt_evt_connection_closed, clear its stale
connection state, and restart connectable advertising with the MyDevice name.
The offline buffer must not prevent advertiser restart.

If recovery fails on hardware, verify all of the following before changing
flow control in software:

- the stable /dev/serial/by-id path resolves to the expected tty;
- no second process owns the resolved tty (sudo fuser -v <tty>);
- baud is 115200 and RTS/CTS is configured consistently on BGM220 NCP and
  J-Link/VCOM, including after a USB power cycle;
- the Pi is not undervolted (vcgencmd get_throttled) and USB power/cable are
  stable;
- EFR Connect can see MyDevice after the old BLE link reaches supervision
  timeout. If it cannot, repair the EFR32 connection_closed advertiser restart.
