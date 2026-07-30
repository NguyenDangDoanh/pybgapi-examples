# BGM220 / Raspberry Pi PAwR contract for BreathSense node 1

## Device roles

- Raspberry Pi: BGAPI host.
- BGM220: Bluetooth NCP, PAwR Advertiser.
- EFR32xG26: PAwR Synchronizer/Responder.
- Ordinary periodic advertising is not used.

## BGM220 identity

- Public MAC: `80:4B:50:54:90:78`
- Address type: `0`
- Advertising SID: `0`
- Integration PHY: LE 1M

## PAwR Advertiser configuration

The Raspberry Pi calls the BGM220 PAwR Advertiser API with:

```text
advertising_set        = 0
interval_min           = 800
interval_max           = 801
flags                  = 2
num_subevents          = 1
subevent_interval      = 80
response_slot_delay    = 40
response_slot_spacing  = 80
response_slots         = 1
```

## Timing interpretation

```text
Periodic interval      = approximately 1000 ms
Subevent               = 0
Response slot          = 0
Response slot count    = 1
```

`flags=2` requests automatic startup of extended advertising.

The Pi/BGM220 application uses:

```text
bt.pawr_advertiser.start()
bt.pawr_advertiser.set_subevent_data()
```

It does not call:

```text
bt.periodic_advertiser.start()
sl_bt_periodic_advertiser_start()
```

## POLL command

POLL is exactly 8 bytes:

```text
[0]     protocol version = 0x01
[1]     opcode = 0x01
[2]     target node = 0x01 or 0xFF
[3]     reserved = 0x00
[4..5]  gateway sequence, uint16 little-endian
[6..7]  reserved = 0x0000
```

Example:

```text
01 01 01 00 34 12 00 00
```

The command is queued with:

```text
advertising_set        = 0
subevent               = 0
response_slot_start    = 0
response_slot_count    = 1
data_length            = 8
```

## RESEND_REQUEST command

RESEND_REQUEST is exactly 8 bytes:

```text
[0]     protocol version = 0x01
[1]     opcode = 0x02
[2]     target node = 0x01
[3]     reserved = 0x00
[4..5]  missing sensor_sequence, uint16 little-endian
[6..7]  reserved = 0x0000
```

## Expected EFR32 response

The EFR32 responds at:

```text
subevent       = 0
response slot  = 0
payload length = 14
```

Telemetry payload:

```text
[0]      magic = 0xB5
[1]      protocol version = 0x01
[2]      node_id = 0x01
[3]      flags
[4..5]   sensor_sequence, uint16 little-endian
[6..7]   temperature, int16 little-endian, unit 0.01 C
[8..9]   humidity, uint16 little-endian, unit 0.01 %RH
[10]     AI class
[11]     AI confidence, 0..100
[12..13] AI sequence, uint16 little-endian
```

A successful BGM220 response report must contain:

```text
subevent      = 0
response_slot = 0
data_status   = 0
data length   = 14
payload       = B5 01 01 ...
```

## sensor_sequence rule

The Si7021 updates approximately every 5 seconds, while BGM220 sends a POLL
approximately every 1 second.

Therefore, several consecutive responses may contain the same
`sensor_sequence`.

Repeated sequence values are valid duplicates, not packet loss.

Packet loss is detected only when `sensor_sequence` advances by more than one.

The Pi may then send a `RESEND_REQUEST` for each missing sequence still
available in the EFR32 16-frame history.
