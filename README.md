_This repository is not affiliated with Powerpal._

# Powerpal Direct for Home Assistant

Connects Home Assistant straight to a Powerpal energy monitor over Bluetooth. No ESP32
gateway and no cloud. Use a Bluetooth adapter on the Home Assistant host, or any
[ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) in range of
the meter box.

This replaces [esphome-powerpal_ble](https://github.com/gurrier/esphome-powerpal_ble). It
also does something the ESP32 gateway couldn't: after any outage it reads back the
readings the Powerpal stored in the meantime, so the energy total has no gaps. That uses
the history protocol worked out in
[Powerpal-protocol](https://github.com/gurrier/Powerpal-protocol).

## What you need

- A Powerpal that's already been set up with the Powerpal app.
- Its **6-digit pairing code**. It's on the card that came with the Powerpal, and in the
  Powerpal app.
- Your meter's pulse rate, printed on the meter as **imp/kWh** (usually 1000).
- Bluetooth in range of the Powerpal: the Home Assistant host's own adapter, or an ESPHome
  Bluetooth proxy with `active: true`. Both can bond (see [Bonding](#bonding)).

**Turn off the ESP32 gateway first** (and close the Powerpal app). The Powerpal accepts one
connection at a time and stops advertising while anything holds it, so Home Assistant
won't even see it until it's free.

> **Tip:** your old gateway ESP32 is already in the right spot. Reflash it as an ESPHome
> Bluetooth proxy (`bluetooth_proxy: active: true`) and this integration can connect
> through it.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories** → add this repository's URL as an **Integration**.
2. Install **Powerpal Direct** and restart Home Assistant.

### Manual

Copy `custom_components/powerpal_direct` into your Home Assistant `config/custom_components/`
folder and restart.

## Setup

The Powerpal should be discovered automatically: look for it under **Settings → Devices &
services**. If it isn't, use **Add integration → Powerpal Direct** to pick it from the
Bluetooth devices in range. If it isn't listed there either, something else is still
connected to it.

Enter the pairing code and pulse rate. Home Assistant bonds with the Powerpal, checks the
code, and creates the device. Counting starts from that moment; history from before setup
isn't imported.

## Bonding

The Powerpal answers nothing until the host it's talking to has bonded with it over
Bluetooth. That bond is separate from the pairing code: the code is checked afterwards, at
the application level. Bonding is what earlier attempts at a direct integration missed.
Without it, every read fails, which looks like a wrong pairing code.

The ceremony is "Just Works", so there's nothing to type. But the Powerpal only accepts a
bond requested *with* man-in-the-middle protection, even though Just Works can't provide
it. On Windows the plain "Encryption" level is refused and only "EncryptionAndAuthentication"
works. Hosts differ in what they ask for:

- **Local adapter (BlueZ):** with no pairing agent registered, BlueZ asks for a bond
  without MITM protection, and the Powerpal fails the pairing with `AuthenticationFailed`.
  That's why earlier direct integrations never got past bonding. While it connects, this
  integration registers a pairing agent with a display-and-keyboard capability, which makes
  BlueZ ask for MITM protection. The pairing is still Just Works, and the agent only
  answers for the Powerpal being connected. Verified on a real Powerpal (firmware
  `1.0.4_3`) through a Home Assistant OS host's Intel adapter: without the agent it failed
  every time, with it it bonded first time.
- **ESPHome Bluetooth proxy:** pairs using the ESP32's own Bluetooth stack and settings,
  the same ones the ESP32 gateway bonded with. Not yet verified through this integration.

Two things the Powerpal needs after bonding, both done automatically, as the ESP32 gateway
did:

- **The pairing code is written with a write request.** The characteristic doesn't accept
  write commands, and on BlueZ a write command is silently ignored.
- **The clock is set.** On first bonding with Home Assistant, the Powerpal was seen to clear
  its clock, its stored history and its reading interval. It then sent no readings until
  its clock was set. The integration sets the clock whenever that's needed: see
  [Keeping the Powerpal's clock right](#keeping-the-powerpals-clock-right).

### Choosing a proxy

Only a proxy that can make connections can reach the Powerpal: an ESP32 running ESPHome
with `bluetooth_proxy: active: true`. ESP32-based smart plugs flashed with ESPHome work well
for this. Passive proxies are fine to keep alongside, but they can only hear the Powerpal,
never connect to it. Home Assistant picks a connectable proxy automatically.

- **Signal:** put the active proxy as close to the meter box as you can. As a guide from
  the ESP32 gateway, aim for better than about -75 dBm; around -85 dBm drops are frequent.
- **Connection slots:** an ESP32 proxy has a few connection slots (3 by default). When
  staying connected, this integration holds one permanently. If other devices need them,
  set a **Catch-up interval** so the slot is only used briefly.

If bonding fails, the integration doesn't keep retrying. It raises a Repairs issue and
tries again every 30 minutes, or when you press **Reconnect**.

## Entities

Named after the ESP32 gateway's entities, so they're familiar if you're moving from
[esphome-powerpal_ble](https://github.com/gurrier/esphome-powerpal_ble).

| Entity | Description |
|---|---|
| Power | Average power over the latest one-minute reading, in W |
| Total Energy | Lifetime total in kWh, kept across restarts. For the Energy dashboard, the hourly statistic is better: see [Energy dashboard](#energy-dashboard) |
| Daily Energy | Total since local midnight, in kWh |
| Timestamp | Time of the newest reading counted, from the Powerpal's own clock |
| Pulses | Meter pulses counted in the latest one-minute reading |
| Watt Hours | Energy in the latest reading, in whole Wh |
| Cost | That energy at your **Cost per kWh** option (0 unless set) |
| Battery | Powerpal battery level |
| LED Sensitivity | The Powerpal's LED sensor sensitivity (1-14, default 9) |
| Device ID | The Powerpal's serial number, which is also its ID in Powerpal's cloud |
| Cloud Upload | Sends your readings to Powerpal's cloud, as the phone app would. **Off by default**; see [Powerpal's cloud](#powerpals-cloud) |
| Last Cloud Upload | When readings last reached Powerpal's cloud |
| BLE Connection | Turn off to release the Powerpal (e.g. so the phone app or an ESP32 gateway can connect). Stays as set across restarts |
| Advertisement RSSI | The Powerpal's signal strength while it's free. Unknown while connected, since a connected Powerpal stops advertising |
| Advertisement Rate | Advertisements heard per minute. Non-zero means the connection was down during that minute |
| Last Stall | What happened the last time the connection was lost, how long the gap was, and how much was recovered from the Powerpal's history. Kept across restarts |
| Connectivity | Whether Home Assistant currently holds the Powerpal's connection |
| Clock Offset | How far the Powerpal's clock is behind Home Assistant's (negative: ahead), in seconds, worked out from when readings arrive. A second or two is normal delivery time |
| Connected Via | The Bluetooth adapter or proxy holding the connection, e.g. `hci0 (18:1D:EA:F5:66:6B)`. Its history shows whenever the connection moves between them. Each one bonds with the Powerpal separately, so frequent moves are worth avoiding |
| Reconnect | Drops the connection and reconnects straight away |

Disabled by default: **Oldest Stored Reading** (how far back the Powerpal's history goes)
and **Set Clock** (sets the Powerpal's clock now; it's normally kept right automatically).

The gateway entities with no counterpart here are the ones about the ESP32 itself (Restart,
ESPHome Version, Heap Free, WiFi Signal, Reset Reason, firmware update, the component
version, and the watchdog's restart count and reason), plus API Key and the button that
retrieved it. The API key is never exposed as an entity: Powerpal Direct reads it from the
Powerpal whenever it needs it. Link RSSI isn't available yet.

## Powerpal's cloud

While Home Assistant holds the Powerpal's connection, the Powerpal app can't connect, so
the app and Powerpal's cloud stop getting your readings. Turn on **Cloud Upload** to send
them from Home Assistant instead:

- Each reading is sent with its own timestamp, including readings recovered from the
  Powerpal's history after an outage.
- It uses the API key and device ID read from the Powerpal itself, so there's nothing to
  copy or keep in `secrets.yaml`.
- If Powerpal's cloud can't be reached, readings wait (across restarts too, up to a week's
  worth) and go up once it's back.
- It stays as you set it across restarts. Turning it off stops sending immediately, and
  drops anything still waiting.

It's off by default because it shares your usage with Powerpal.

If you already upload readings with your own automation and `rest_command` (common with the
ESP32 gateway), disable that automation before turning this on, or readings will be sent
twice. See [Migrating from the ESP32 gateway](#migrating-from-the-esp32-gateway).

## Migrating from the ESP32 gateway

Powerpal Direct can take over the gateway's entity IDs, so history, long-term statistics,
the Energy dashboard, utility meters and automations all carry on without editing anything.

1. **Turn off any automation that uploads readings to Powerpal's cloud** with a
   `rest_command` (Settings → Automations & scenes → the automation → ⋮ → **Disable**).
   Do this first. Powerpal Direct takes over the gateway's Pulses, Watt Hours and Cost
   entity IDs, so such an automation would carry on firing on the new sensors and send
   every reading twice: once from it, and once from **Cloud Upload** if you turn that on.
   Once Cloud Upload is working, the automation, its `rest_command` and any API key kept in
   `secrets.yaml` for it can be deleted. Powerpal Direct reads the key from the Powerpal.
2. **Remove the gateway's device** from Home Assistant: Settings → Devices & services →
   ESPHome → the gateway → ⋮ → **Delete**, and power the ESP32 off (or reflash it as a
   Bluetooth proxy). Removing it frees its entity IDs.
3. **Add Powerpal Direct.** Each entity that has a gateway counterpart is created under the
   gateway's old entity ID, found by name (`Powerpal Total Energy` and so on), whatever its
   device or area prefix was. Setup also works out the gateway's pulses per kWh from its
   recorded Pulses and Watt Hours, and fills it in for you to confirm. It must match the
   gateway's, or the total carried over is rescaled. Cost per kWh is carried over the same
   way.
4. **It carries on from the gateway's last readings.** Total Energy starts from the
   gateway's last Total Energy, rather than zero. Today's Daily Energy carries on too. The
   gateway's last Timestamp becomes the starting point, so the first catch-up fetches the
   minutes since the gateway stopped from the Powerpal's own history.

Things to know:

- Only entities that don't exist yet are created this way. If Powerpal Direct was already
  set up alongside the gateway, delete Powerpal Direct as well, then add it again after
  step 2. The Bluetooth bond is kept, so it only needs the pairing code again.
- The gateway's entities are recognised by the names the ESPHome component documents. If
  you renamed them in your YAML, rename Powerpal Direct's entities by hand instead
  (Settings → Entities → the entity → entity ID). Home Assistant then attaches the old
  history to the new entity.
- Home Assistant remembers removed entities for 30 days, so add Powerpal Direct within
  that time of removing the gateway.
- Pulses, Watt Hours and Cost now have units, so from the switch-over on they're graphed as
  numbers. Their earlier history is kept.

## Energy dashboard

Powerpal Direct also writes a long-term statistic, **Powerpal … Energy (hourly)**, that
books every reading to the hour it was measured in. Readings recovered after an outage go
back into the hours they belong to, rather than the hour they arrived.

Which to use as the grid consumption source in Settings → Dashboards → Energy:

- **Total Energy (the sensor)**, if the Energy dashboard calculates your costs from a price
  entity or a fixed price. Home Assistant only works out costs that way for an entity, and
  the hourly statistic isn't one: with it, costs would silently stop. In normal running,
  readings arrive within a minute or so of being measured, so the sensor's hours are only
  off around an outage.
- **The hourly statistic**, if you don't track costs in the Energy dashboard, or want the
  hours exact after outages above all else. It also appears in statistics graph cards,
  whichever you choose.

- When it's first created, it copies the hourly history the Total Energy sensor already
  has, so switching the Energy dashboard over leaves no gap. After taking over from an
  ESP32 gateway, that's the gateway's history.
- On a brand-new install, it also imports whatever history the Powerpal holds, so the
  Energy dashboard starts with that. Total Energy itself still starts from zero.
- It's separate from Total Energy's own statistics, which are untouched, along with
  anything built on them, such as utility meters.

## Keeping the Powerpal's clock right

Every reading is stamped by the Powerpal's own clock, and the Powerpal sends nothing at all
until its clock has been set. Rather than set it on a timer (the ESP32 gateway did every 6
hours), Powerpal Direct sets it only when needed:

- on connecting, when the Powerpal's history is empty, which is what it looks like after a
  reset or a new bond;
- when a connection gets no reading at all shortly after connecting;
- when the clock has drifted more than a minute from Home Assistant's, judged from when
  live readings arrive (shown as **Clock Offset**), at most every 6 hours.

**Set Clock** (off by default) sets it on demand. Reading `59DA0004` back doesn't tell you
the time: it returns the last value written.

## Options

| Option | Default | Description |
|---|---|---|
| Meter pulses per kWh | 1000 | Changing it rescales the whole total, not just new readings |
| Catch-up interval | 0 | `0` stays connected for live readings, as the ESP32 gateway did. Any other value connects every that many minutes, fetches the readings stored since the last visit, and disconnects. Power then only updates at each visit, but the Bluetooth connection slot is free in between |
| Live reading interval | 1 | When staying connected: minutes between the Powerpal's live readings |
| Cost per kWh | 0 | Flat rate for the **Cost** sensor |

## How it works

1. **Bond.** Standard BLE bonding, "Just Works", so there's nothing to enter. The Powerpal
   refuses every read until it's bonded.
2. **Authenticate.** The pairing code is written to `59DA0011` as a little-endian uint32.
   This is an application-level check, separate from the bond. Reading the API key from
   `59DA0009` confirms it worked.
3. **Catch up.** `59DA0005` gives the oldest and newest stored readings. Everything newer
   than the last reading already counted is requested by writing a `[start][end]` window to
   `59DA0002`. The Powerpal replays it as notifications on `59DA0001`, one per minute of
   history.
4. **Stream** (when staying connected). Live readings keep arriving on `59DA0001`. If one
   shows minutes went missing while the link stayed up, those are fetched from history
   before it's counted. If readings stop altogether, the integration reconnects.

The last reading counted is saved with the totals, and every reading carries its own
timestamp, so replaying history never counts a minute twice. Readings are booked to the day
they were measured on, so history replayed after midnight lands in yesterday, not today.

### Things to know

- **Total Energy's own statistics put recovered energy in the hour it was recovered.**
  Home Assistant compiles a sensor's statistics from when its state changes, so after an
  outage they show the recovered energy in the hour catch-up happened. The total is right.
  For the hour-by-hour split, use the hourly statistic: see [Energy dashboard](#energy-dashboard).
- **An outage longer than the Powerpal's stored history loses the oldest minutes.** The
  log says how many. How much the Powerpal keeps isn't known yet; it's been seen holding
  over 12 hours.
- **Bonding a new host may reset the Powerpal.** When Home Assistant first bonded, the
  Powerpal's stored history was empty and its clock unset. Expect to lose any readings it
  held from before. If your phone app has trouble afterwards, pair the phone again.
- Nothing ever writes to the Nordic DFU service (`0xFE59`), which is the firmware update
  path.

## Troubleshooting

Turn on debug logging for `custom_components.powerpal_direct` (and
`bleak_retry_connector` for connection problems), and download diagnostics from the
device page. Diagnostics include the last catch-up and the saved counters, with the
pairing code and API key redacted.

- **"Pairing code not accepted" / reauthentication requested**: the link is bonded but
  the code was rejected. Check the code.
- **"Can't bond over Bluetooth" Repairs issue**: see [Bonding](#bonding). Use an ESPHome
  Bluetooth proxy, and on a local adapter remove the stale record with
  `bluetoothctl remove <address>`.
- **Proxy says pairing isn't available**: update the proxy's ESPHome firmware.
- **"Only heard by … which can't make connections" in the log**: the scanner that hears the
  Powerpal is passive-only. Add `active: true` to the proxy, or move a connectable adapter
  or proxy within range.
- **Frequent disconnects**: the signal is too weak. Move the adapter or proxy closer, or
  add a proxy near the meter box.

## Development

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

The tests run against a simulated Powerpal (`tests/fakes.py`) that replays history the
way the real one does, including live readings crossing a history reply.

## Credits

Builds on the protocol work of WeekendWarrior1 and muneeb1990, the
[esphome-powerpal_ble](https://github.com/gurrier/esphome-powerpal_ble) component, and the
history protocol documented in [Powerpal-protocol](https://github.com/gurrier/Powerpal-protocol).
