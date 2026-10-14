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
- Bluetooth in range of the Powerpal. **An ESPHome Bluetooth proxy with `active: true` is
  recommended** (see [Bonding](#bonding) for why). A local adapter on the Home Assistant
  host may also work.

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

- **ESPHome Bluetooth proxy:** pairs using the ESP32's own Bluetooth stack and settings,
  the same ones the ESP32 gateway bonded with. This is the path most likely to work.
- **Local adapter (BlueZ):** Home Assistant pairs without a pairing agent, so BlueZ
  requests a bond with no protection, which the Powerpal may refuse. There are reports of
  BlueZ pairing failing with `AuthenticationFailed`, and of the failed attempt leaving a
  record that blocks later connections until it's removed with
  `bluetoothctl remove <address>`.

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

| Entity | Description |
|---|---|
| Power | Average power over the latest one-minute reading, in W |
| Energy | Lifetime total in kWh, kept across restarts. **Use this one in the Energy dashboard** |
| Energy today | Total since local midnight, in kWh |
| Battery | Powerpal battery level |
| Connectivity | Whether Home Assistant currently holds the Powerpal's connection |
| Last reading | Time of the newest reading counted, from the Powerpal's own clock |
| LED sensitivity | The Powerpal's LED sensor sensitivity (1-14, default 9) |
| Reconnect | Drops the connection and reconnects straight away |

Disabled by default: **Pulses in last reading**, **Oldest stored reading** (how far back
the Powerpal's history goes), **Cloud API key** (for Powerpal's cloud API, together with
the device's serial number, shown on the device page) and **Set clock to now**.

## Options

| Option | Default | Description |
|---|---|---|
| Meter pulses per kWh | 1000 | Changing it rescales the whole total, not just new readings |
| Catch-up interval | 0 | `0` stays connected for live readings, as the ESP32 gateway did. Any other value connects every that many minutes, fetches the readings stored since the last visit, and disconnects. Power then only updates at each visit, but the Bluetooth connection slot is free in between |
| Live reading interval | 1 | When staying connected: minutes between the Powerpal's live readings |

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

- **Energy recovered after an outage is recorded when it's recovered.** Home Assistant's
  statistics put it in the hour catch-up happened, not the hours it was used in. The
  lifetime total is right; the hourly split around an outage isn't.
- **An outage longer than the Powerpal's stored history loses the oldest minutes.** The
  log says how many. How much the Powerpal keeps isn't known yet; it's been seen holding
  over 12 hours.
- **Set clock to now** is off by default. The Powerpal keeps good time on its own, and
  moving its clock shifts the timestamps that catch-up relies on. Use it only if the
  clock is clearly wrong, e.g. after a battery change. Note that reading `59DA0004` back
  doesn't tell you the time: it returns the last value written, not a running clock.
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
