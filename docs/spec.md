# Combustion Inc BLE Protocol (Probe + MeatNet + Gauge)

This document consolidates the public Combustion Inc BLE protocol documentation into an implementation-oriented spec for this repository.

Scope:
- Predictive Probe BLE spec (advertising, GATT, UART-over-NUS)
- MeatNet Node BLE spec (repeater/mesh advertising + UART-over-NUS)
- Gauge BLE spec (new device; a MeatNet repeater node with additional gauge-specific payload/messages)

Primary upstream references:
- Probe: https://github.com/combustion-inc/combustion-documentation/blob/main/probe_ble_specification.rst
- MeatNet Node: https://github.com/combustion-inc/combustion-documentation/blob/main/meatnet_node_ble_specification.rst
- Gauge: https://github.com/combustion-inc/combustion-documentation/blob/main/gauge_ble_specification.rst

Notes:
- The upstream docs are marked “DRAFT”. Treat this as authoritative, but validate against BLE captures when something is ambiguous.
- This file paraphrases/summarizes; it is not a verbatim copy of upstream docs.

---

## 1) Shared constants and concepts

### 1.1 Bluetooth Manufacturer Specific Data (MSD)

All devices described here broadcast BLE 4.0 advertisements that include Manufacturer Specific Data (MSD).

- Combustion Inc Bluetooth Company Identifier: `0x09C7`.
- In BLE MSD, the vendor/company ID is the first 2 bytes.

### 1.2 Product Type

`Product Type` is a 1-byte enum used in MSD and in mesh messages.

Known values (from MeatNet node spec):
- `0`: Unknown
- `1`: Predictive Probe
- `2`: MeatNet Repeater Node (used in advertisements to show repeated probe data)
- `3`: Giant Grill Gauge
- `4`: Display (Timer)
- `5`: Booster (Charger)

(Other device specs may define additional values; e.g., Engine uses `6`.)

### 1.3 “Direct vs repeated” probe data

A probe can advertise its own state.

Separately, a MeatNet repeater node (including Gauge) can re-advertise “probe-like” MSD frames for probes on its network. These repeated frames share the same core layout as probe MSD and add a usable `Network Information` byte (including hop count).

This matters for HA:
- Passive scan can give you temperatures and some state.
- Connecting to a probe gives richer state (prediction, alarms, logs, food safe status), via a custom characteristic and/or UART messages.
- Connecting to a node/gauge lets you query topology and proxy commands to probes.

---

## 2) Probe: BLE advertising

### 2.1 Advertising behavior

- The probe advertises continuously when not in its charger.
- It supports up to 3 simultaneous BLE connections.
- When it has fewer than 3 connections it uses connectable advertising; otherwise unconnectable.
- Typical advertising intervals:
  - Instant Read: repeating pattern of ~100ms, 100ms, 50ms between three messages
  - Normal: ~250ms

### 2.2 Probe scan response

- Probe scan response includes a 16-byte “Service UUID” for the Probe Status Service.

### 2.3 Probe MSD layout (24 bytes)

This is the `Manufacturer Specific Data` payload length, including the vendor ID.

Offsets are byte offsets within MSD:

| Offset | Size | Field | Notes |
|---:|---:|---|---|
| 0..1 | 2 | Vendor ID | Must be `0x09C7` (big-endian in the docs) |
| 2 | 1 | Product Type | Must be `1` for probe |
| 3..6 | 4 | Serial Number | Probe serial number (little-endian in the existing integration code) |
| 7..19 | 13 | Raw Temperature Data | Packed 8×13-bit thermistor readings |
| 20 | 1 | Mode/ID | Packed bitfield: mode, color ID, probe ID |
| 21 | 1 | Battery Status + Virtual Sensors | Low-battery + virtual sensor mapping |
| 22 | 1 | Network Information | Unused by probe; “don’t care” |
| 23 | 1 | Overheating Sensors | Bitmask for which sensors are overheating |

---

## 3) MeatNet Node: BLE advertising (repeated probe data)

### 3.1 Node advertising behavior

- Node advertises continuously when powered on.
- Supports up to 4 simultaneous incoming BLE connections and up to 4 outgoing.
- If it has fewer than 4 incoming connections: connectable advertising; otherwise unconnectable.
- Interval:
  - If any probe on the network is in Instant Read: ~100ms
  - Else: ~250ms

### 3.2 Node scan response

- Node scan response includes the DFU service UUID (details are “TBD” upstream).

### 3.3 Node MSD layout (24 bytes; “repeated probe”)

The node interleaves advertisements for each probe on its network, cycling through them one-by-one. The fields match the probe MSD with one key difference: `Network Information` is meaningful.

| Offset | Size | Field | Notes |
|---:|---:|---|---|
| 0..1 | 2 | Vendor ID | `0x09C7` |
| 2 | 1 | Product Type | `2` (node repeated-probe advertisement)
| 3..6 | 4 | Serial Number | Probe serial number that this repeated data pertains to |
| 7..19 | 13 | Raw Temperature Data | Same format as probe |
| 20 | 1 | Mode/ID | Same format as probe |
| 21 | 1 | Battery Status + Virtual Sensors | Same format as probe |
| 22 | 1 | Network Information | Includes hop count (see below) |
| 23 | 1 | Overheating Sensors | Same format as probe |

### 3.4 Network Information (hop count)

This 1-byte packed field is defined as:

- Bits 1–2: Hop Count
- Bits 3–8: Reserved

Hop Count encoding:
- `0` → 1 hop
- `1` → 2 hops
- `2` → 3 hops
- `3` → 4 hops

Interpretation:
- “The number of repeater network hops from the Probe for which this data pertains.”

Practical guidance for HA:
- Use this as a bounded “distance” / “via mesh” signal.
- If you see the same probe data repeated from multiple nodes, prefer the lower hop count when picking which advertisement to surface.

---

## 4) Gauge: BLE advertising and differences

Gauge is a MeatNet repeater node that additionally emits Gauge-specific advertisements and supports gauge-specific UART messages.

### 4.1 Gauge advertising

- Gauge interleaves Gauge-specific advertisements between MeatNet node advertisements.
- MeatNet node advertisements include repeated probe data (as described above).

### 4.2 Gauge MSD layout (24 bytes)

| Offset | Size | Field | Notes |
|---:|---:|---|---|
| 0..1 | 2 | Vendor ID | `0x09C7` |
| 2 | 1 | Product Type | `3` for Gauge |
| 3..12 | 10 | Serial Number | Gauge serial number (alphanumeric byte array) |
| 13..14 | 2 | Raw Temperature Data | Packed 13-bit temperature, 0.1°C steps |
| 15 | 1 | Gauge Status Flags | Sensor present, overheating, low battery |
| 16 | 1 | Reserved | Reserved |
| 17..20 | 4 | High-Low Alarm Status | Packed two 16-bit Alarm Status fields |
| 21..23 | 3 | Reserved | Reserved |

### 4.3 Gauge Status Flags (1 byte)

Packed bits:
- Bit 1: Sensor Present
- Bit 2: Sensor Overheating
- Bit 3: Low Battery
- Bits 4–8: Reserved

---

## 5) Shared data formats (Probe + Node repeated-probe + Probe status)

### 5.1 Raw Temperature Data (probe thermistors; 13 bytes)

- 13 bytes (104 bits) encode 8 thermistor readings.
- Each reading is 13-bit unsigned.

Bit layout (1-indexed in upstream docs):
- Bits 1–13: Thermistor 1
- 14–26: Thermistor 2
- 27–39: Thermistor 3
- 40–52: Thermistor 4
- 53–65: Thermistor 5
- 66–78: Thermistor 6
- 79–91: Thermistor 7
- 92–104: Thermistor 8

Conversion (°C):
- $T = (raw \times 0.05) - 20$
- Stated range: -20°C .. 369°C

Important behavior:
- If Mode is Normal: all 8 sensors present.
- If Mode is Instant Read: Thermistor 1 field contains the instant-read temperature; other thermistors are 0.

### 5.2 Mode and ID Data (1 byte)

Packed bits:
- Bits 1–2: Mode
  - `0`: Normal
  - `1`: Instant Read
  - `2`: Reserved
  - `3`: Error
- Bits 3–5: Color ID (0–7)
  - `0`: Yellow
  - `1`: Grey
  - `2–7`: TBD
- Bits 6–8: Probe Identifier
  - `0`: ID 1
  - `1`: ID 2
  - …

### 5.3 Virtual sensors

Virtual sensors identify which physical thermistor should be treated as “core”, “surface”, and “ambient”.

- Virtual Core Sensor (3 bits):
  - `0`: T1 (tip)
  - `1`: T2
  - `2`: T3
  - `3`: T4
  - `4`: T5
  - `5`: T6
- Virtual Surface Sensor (2 bits):
  - `0`: T4
  - `1`: T5
  - `2`: T6
  - `3`: T7
- Virtual Ambient Sensor (2 bits):
  - `0`: T5
  - `1`: T6
  - `2`: T7
  - `3`: T8

### 5.4 Battery Status and Virtual Sensors (1 byte)

Packed bits:
- Bit 1: Battery Status
  - `0`: Battery OK
  - `1`: Low battery
- Bits 2–8: Virtual Sensors

Doc caveat:
- One part of the upstream docs describes “Virtual sensors are expressed in a packed 5-bit field” but then enumerates core (3 bits) + surface (2 bits) + ambient (2 bits) = 7 bits. In practice, treat “bits 2–8 contain virtual sensor mapping” as the authoritative definition.

### 5.5 Overheating Sensors (1 byte)

- Bitmask for overheating thermistors.
- MSB corresponds to T8, LSB corresponds to T1.

---

## 6) Probe “rich state”: GATT services and the Probe Status characteristic

### 6.1 Device Information Service (standard)

- Service UUID: `0x180A`
- Characteristics (read): manufacturer name, model number, serial number, hardware revision, firmware revision.

### 6.2 Probe Status Service (custom; probe only)

- Service UUID: `00000100-CAAB-3792-3D44-97AE51C1407A`
- Characteristic UUID: `00000101-CAAB-3792-3D44-97AE51C1407A` (Read, Notify)

The notification payload (“Probe Status”) includes (high level):
- Log Range: two uint32 sequence numbers (min, max)
- Current Raw Temperature Data (13 bytes)
- Mode/ID (1 byte)
- Battery Status and Virtual Sensors (1 byte)
- Prediction Status (7 bytes)
- Food Safe Data (10 bytes)
- Food Safe Status (8 bytes)
- Overheating Sensors (1 byte)
- Thermometer Preferences (1 byte)
- High Alarm Status array (11×uint16 = 22 bytes): T1..T8, Core, Surface, Ambient
- Low Alarm Status array (11×uint16 = 22 bytes): same ordering

### 6.3 Nordic UART Service (NUS) (custom)

All three devices in-scope (Probe, MeatNet Node, Gauge) use Nordic UART Service:

- Service UUID: `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`
- RX (Write): `6E400002-B5A3-F393-E0A9-E50E24DCCA9E`
- TX (Read/Notify): `6E400003-B5A3-F393-E0A9-E50E24DCCA9E`

---

## 7) UART-over-NUS message framing

### 7.1 CRC

CRC is CRC-16-CCITT:
- Polynomial: `0x1021`
- Initial value: `0xFFFF`

### 7.2 Probe UART header

Probe request header (6 bytes):
- Sync bytes: `{0xCA, 0xFE}` (2 bytes)
- CRC (2 bytes): CRC over (message type + payload length + payload)
- Message type (1 byte)
- Payload length (1 byte)

Probe response header (7 bytes):
- Sync bytes `{0xCA, 0xFE}` (2)
- CRC (2): same CRC definition
- Message type (1)
- Success (1): 1 success, 0 failure
- Payload length (1)

### 7.3 MeatNet Node UART header

Node request header (10 bytes):
- Sync bytes `{0xCA, 0xFE}` (2)
- CRC (2): CRC over (message type + request ID + payload length + payload)
- Message type (1): MSB = 0 for requests
- Request ID (4): random unique ID for network propagation
- Payload length (1)

Node response header (15 bytes):
- Sync bytes `{0xCA, 0xFE}` (2)
- CRC (2): CRC over (message type + request ID + response ID + success + payload length + payload)
- Message type (1): MSB = 1 for responses
- Request ID (4): from original request
- Response ID (4): random unique ID
- Success (1)
- Payload length (1)

Implementation implications:
- A node/gauge connection requires request/response correlation (Request ID, Response ID).
- Node also emits notifications without responses (e.g., topology/heartbeat and “device connected/disconnected”).

---

## 8) Probe UART messages (common operations)

These are direct-probe commands (and are also proxied through MeatNet nodes by prepending probe serial number in the node request payload).

Common message types:
- `0x01` Set Probe ID
- `0x02` Set Probe Color
- `0x03` Read Session Information
- `0x04` Read Logs (streamed responses)
- `0x05` Set Prediction
- `0x06` Read Over Temperature
- `0x07` Configure Food Safe
- `0x08` Reset Food Safe
- `0x09` Set Power Mode
- `0x0A` Reset Thermometer
- `0x0B` Set Probe High/Low Alarm arrays
- `0x0C` Silence Alarms

If we only need passive sensing in HA, we can ignore most of these. If we want prediction/alarms/logs configuration in HA, these become relevant.

---

## 9) MeatNet Node UART messages (mesh features)

In addition to proxying many probe operations, the node spec defines messages for mesh events and topology:

- `0x40` Device Connected (notification)
- `0x41` Device Disconnected (notification)
- `0x42` Read Node List (paged)
- `0x43` Read Network Topology
- `0x44` Read Probe List
- `0x45` Probe Status (notification for a probe on the network; includes `Network Information`)
- `0x46` Probe Firmware Revision
- `0x47` Probe Hardware Revision
- `0x48` Probe Model Information
- `0x49` Heartbeat Message (notification; includes hop count for the message itself and per-connection RSSI)
- `0x4A` Associate Node
- `0x4B` Sync Thermometer List

Hop count appears in multiple places:
- Advertising repeated-probe `Network Information` hop count indicates hops “from the probe for which this data pertains.”
- Heartbeat hop count indicates how many hops that heartbeat message has taken.

---

## 10) Gauge UART messages (new device)

Gauge supports all MeatNet node messages plus these gauge-specific ones:

- `0x60` Gauge Status (notification)
  - Includes: serial (10), session ID (4), sample period (2), raw temp (2), flags (1), log range (8), alarms (4), “new record flag” (1), network info (1)
- `0x61` Set Gauge High/Low Alarm (high/low each 16-bit Alarm Status)
- `0x62` Read Gauge Logs (streamed responses)

Gauge temperature encoding:
- 13-bit packed, 0.1°C resolution
- $T = (raw \times 0.1) - 20$
- If sensor not present, raw value is 0.

---

## 11) “All probe properties” checklist (what should HA be able to represent)

### 11.1 From passive scanning (probe advertising OR node repeated-probe advertising)

Per-probe (identified by serial number):
- T1..T8 temperatures (°C), with Instant Read special behavior
- Mode (Normal / Instant Read / Error)
- Color ID
- Probe Identifier
- Low battery flag
- Virtual sensor mapping (core/surface/ambient source thermistors)
- Overheating bitmask (T1..T8)

Only from node repeated-probe advertising:
- Hop count (1–4) describing mesh distance from the probe

### 11.2 From direct probe connection (Probe Status characteristic)

Everything above, plus:
- Log range (min/max sequence numbers)
- Prediction status (state/mode/type, set point, heat start temp, time-to-removal/resting, estimated core temperature)
- Food-safe configuration and status (including log reduction + seconds above threshold + program state)
- Thermometer power mode
- Alarm status arrays (high/low) for: T1..T8 + Core + Surface + Ambient

### 11.3 From MeatNet node connection (optional)

- Probe Status notifications for probes on the network (includes network info)
- Probe lists, node lists, and topology (including per-connection RSSI)

---

## 12) Hop count: answering “how many hops away is the probe?”

Best-effort approach:
- If you received the probe state from node repeated-probe advertising (product type = 2), parse `Network Information` (byte 22) and extract bits 1–2.
- Map `0..3 → 1..4 hops`.

Limitations:
- If you receive data directly from a probe’s own advertising, its `Network Information` is unused and you cannot derive hop distance from that frame.
- Topology messages can provide richer mesh understanding, but require a connection to a node/gauge.

---

## 13) Integration notes / sanity checks

These are repo-specific “things to verify” when comparing our code to the spec:

- MSD length: upstream spec uses 24-byte MSD for probe and node repeated-probe advertisements. Parsers should tolerate shorter frames only if they explicitly support partial decoding.
- Probe serial number endianness: the upstream tables do not explicitly call out endianness for all integer fields. Our code currently parses probe serial as little-endian; verify with real captures.
- Battery + virtual sensors bit widths: upstream text contains an internal inconsistency; treat the bit allocation used by the rest of the spec (battery bit + 7 bits virtual sensors) as authoritative.

---

## 14) Future extensions

Planned additions (not fully documented here yet):
- Repeaters / additional node types beyond the MeatNet node baseline.
- “New gauge” device behavior beyond what’s currently in the published gauge spec.

When we add these, extend:
- Product Type enum coverage
- Additional advertising layouts (if any)
- Additional UART message types and payload structures
