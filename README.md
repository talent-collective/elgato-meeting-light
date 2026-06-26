# elgato-meeting-light

Automatically turns your Elgato Key Light on when your camera activates and off when it deactivates — no manual toggling during calls.

Works on **Windows** and **macOS**. No configuration needed; the light is discovered automatically on your local network.

## How it works

| Part | Mechanism |
|------|-----------|
| Camera detection (Windows) | Polls the Windows privacy registry (`CapabilityAccessManager`) every 2 seconds — the same signal that drives the OS camera indicator dot |
| Camera detection (macOS) | Reads Control Center's privacy-indicator log (`sensor-indicators` → `"Active activity attributions changed to […]"`) — the same signal that drives the green camera dot. A `cam:` entry means the camera is live; a mic-only session is ignored |
| Light discovery | mDNS/Bonjour (`_elg._tcp.local.`) — finds the light automatically, no IP address needed |
| Light control | `PUT http://<light-ip>:9123/elgato/lights` — toggles on/off without changing your brightness or temperature preset |

## Requirements

- Python 3.8+
- Elgato Key Light or Key Light Air
- Elgato Control Center installed and running (it advertises the light on your network)

## Installation

### Windows

```powershell
git clone https://github.com/talent-collective/elgato-meeting-light.git
cd elgato-meeting-light
.\setup.ps1
```

To uninstall:
```powershell
.\setup.ps1 -Uninstall
```

### macOS

```bash
git clone https://github.com/talent-collective/elgato-meeting-light.git
cd elgato-meeting-light
bash setup.sh
```

To uninstall:
```bash
bash setup.sh --uninstall
```

## Manual usage

```bash
# Smoke test — discovers the light and toggles it once
python main.py --test

# Run in the foreground
python main.py
```

Logs are written to `elgato-light.log` in the project directory.

## Startup behavior

| Platform | Mechanism |
|----------|-----------|
| Windows | Registry `HKCU\...\Run` key (no admin required) |
| macOS | LaunchAgent in `~/Library/LaunchAgents/` |

The process restarts automatically if it crashes.

## Notes

- Brightness and color temperature are never changed by this tool — your Control Center preset is always preserved.
- If the light is powered off or unplugged, the script will keep watching for it to reappear on the network and reconnect automatically.
- On macOS, camera detection uses Control Center's privacy-indicator signal (the green-dot source), so it reflects the camera's true state rather than a helper process that can linger after the camera is released. Requires macOS 12+.

## License

MIT
