# Sensa Control

Sensa Control turns your Home Assistant into a purpose-built control system for
your vehicle. It scans your network and Bluetooth for supported devices,
registers them, and generates your dashboards and controls automatically.
Everything runs locally on your own Home Assistant, with no cloud dependency for
day-to-day use.

For the full user guide, visit [docs.sensa.systems](https://docs.sensa.systems).

## Getting started

1. Install and start this add-on.
2. Add the **Sensa Nomad** integration (Settings, then Devices & Services, then
   Add Integration, then Sensa Nomad). It talks to this add-on on your local
   network.
3. The guided setup scans for supported devices and builds your dashboards. Tabs
   appear only for the devices you actually have.
4. On first run a **14-day free trial** is provisioned automatically, bound to
   your hardware. No code is needed to start.
5. When you buy a licence, enter your code (`SENSA-XXXX-XXXX-XXXX`) to unlock the
   device permanently. It is activated online once, then validated offline for
   good, so no internet connection is needed after that.

When a trial or licence expires the system becomes read-only: you can still view
every dashboard, but controls are disabled until you renew.

## Configuration

Most installs need no configuration changes. The defaults point at the correct
Sensa licensing service for the channel you installed.

| Option | Description |
|--------|-------------|
| `licence_api_url` | The Sensa licensing service URL. Leave as the default unless Sensa support asks you to change it. |
| `licence_public_key` | The key used to verify your licence offline. It is already set to the correct key for the channel you installed, so leave it as is. Do not clear it: a blank value falls back to a development key that cannot verify beta or production licences, and activation then fails with a signature error. Only change it if Sensa support tells you the signing keys have rotated. |
| `licence_data_path` | Where licence state is stored. Defaults to `/data` and should not normally be changed. |
| `deployment_mode` | How this is installed. `byo` for installing onto your own Home Assistant (the default). `enthusiast` (Home Assistant OS) and `appliance` (Container) are set by Sensa on factory-built appliances only. |

## What you get

- A guided setup that discovers your devices and generates dashboards for you.
- Purpose-built panels rather than hand-built Lovelace dashboards: Habitation
  (your everyday hub), Cab, a condensed Mobile view, Intelligence, and Settings.
- A live Power Central home screen with battery and water-tank gauges, an
  animated power-flow diagram, and a 24-hour solar chart.
- Daily automatic backups and a one-tap file integrity check, built in.

## Support

- User guide and troubleshooting: [docs.sensa.systems](https://docs.sensa.systems)
- Buy a licence: [shop.sensa.systems](https://shop.sensa.systems)

If a device is not discovered, check that it is powered and on the same network,
and that any required Bluetooth source (such as a Sensa Bluetooth Proxy) is
present. Bluetooth devices will not be discovered without a Bluetooth source.
