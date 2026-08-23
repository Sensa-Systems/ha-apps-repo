# Release 1.0.4

The first full Sensa Nomad release. Plug it in, let it find your vehicle's
devices, and it builds your dashboards and controls for you. Everything runs
locally on your own Home Assistant.

## Highlights

- Guided setup that scans your network and Bluetooth for supported devices,
  registers them, and generates your dashboards automatically.
- Purpose-built panels rather than fiddly Lovelace dashboards: Habitation (your
  everyday hub), Cab, a condensed Mobile view, and Settings. Tabs appear only
  for the devices you actually have.
- A live Power Central home screen with battery and water-tank gauges, an
  animated power-flow diagram, and a 24-hour solar chart.
- 14-day free trial, then a one-time licence code unlocks the device for good.
  Activated online once, validated offline forever, so no internet is needed
  day to day.
- Daily automatic backups and a one-tap file integrity check, built in.

## Supported devices

### Power and battery
- Victron Cerbo GX (Venus OS over MQTT), SmartShunt, SmartSolar MPPT and
  Orion XS DC-DC charger
- Leisure battery BMS over Bluetooth: Roamer (Gen 1 and Gen 2) and Fogstar Drift
- Full electrical power-flow view, including shore power and dual AC output

### Connectivity
- Teltonika routers and managed switches: full network view, GPS, port status
  and PoE
- Starlink dish monitoring, with signal quality and an on-demand speed test
- Sensa Bluetooth Proxy (C124 and C008 models)

### Climate and comfort
- Velit rooftop air conditioner, with an interactive thermostat card
- Govee and Ruuvi environment sensors, with cabin temperature chips that appear
  automatically

### Switching and levelling
- Relay controller with per-channel naming, custom switch images and a
  device-type assignment for each channel
- Van levelling sensor

## Notes

- Bluetooth devices need a Bluetooth source present first (a Sensa Bluetooth
  Proxy or a compatible adapter), otherwise they will not be discovered.
- When a trial or licence expires the system becomes read-only: you can still
  view every dashboard, but controls are disabled until you renew.
- A few device cards still rely on HACS helper cards; native Sensa replacements
  are rolling out.
