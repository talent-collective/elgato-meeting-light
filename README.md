# elgato-meeting-light

Turns an Elgato Key Light on when your camera is in use and off when it is not.

Works on **macOS** and **Windows**. The light is discovered on the local network. Brightness and color temperature are left alone.

## Install or reinstall on a Mac

Paste this. Running it again reinstalls the background service and sets the light to match the camera (it stays **off** unless the camera is actually in use):

```bash
curl -fsSL https://raw.githubusercontent.com/talent-collective/elgato-meeting-light/master/setup.sh | bash
```

From a checkout of this repo, the same install is `bash setup.sh`.

Uninstall:

```bash
curl -fsSL https://raw.githubusercontent.com/talent-collective/elgato-meeting-light/master/setup.sh | bash -s -- --uninstall
```

| | |
|---|---|
| LaunchAgent | `com.elgato-meeting-light` |
| Plist | `~/Library/LaunchAgents/com.elgato-meeting-light.plist` |
| Log | `~/Library/Logs/elgato-meeting-light.log` |
| Startup errors | `~/Library/Logs/elgato-meeting-light.launchd.err.log` |

The agent loads at login (`RunAtLoad`) and is restarted if it exits (`KeepAlive`). It runs the Python in `~/.elgato-meeting-light/.venv` (or the checkout's `.venv` if you installed from a clone), so launchd does not depend on your shell `PATH`.

Watch camera transitions:

```bash
tail -f ~/Library/Logs/elgato-meeting-light.log
```

Lines look like `Camera state changed: off -> on (Frame publisher: us.zoom.xos)` and `Light ON` / `Light OFF`.

### macOS permissions

- **Local Network.** Allow it if macOS prompts. The light is controlled with `PUT http://<light>:9123/elgato/lights`. Without this, discovery or the light command fails.
- **Full Disk Access.** Not required for a normal install. If the log says the system log is not permitted, add the virtualenv Python (`~/.elgato-meeting-light/.venv/bin/python`) under System Settings → Privacy & Security → Full Disk Access, then run the install command again.
- **Camera.** Do not grant it. This program never opens the camera. Camera access is not how state is detected, and granting it is not required.

Elgato Control Center needs to be running so the light is advertised on the network.

## Windows

```powershell
git clone https://github.com/talent-collective/elgato-meeting-light.git
cd elgato-meeting-light
.\setup.ps1
```

Uninstall: `.\setup.ps1 -Uninstall`

The startup task is `ElgatoMeetingLight` (runs at login). Logs: `elgato-light.log` in the project directory.

## How it works

| Part | Mechanism |
|------|-----------|
| Camera detection (macOS) | Unified log via `/usr/bin/log`, not the `VDCAssistant` / `AppleCameraAssistant` processes. Those helpers stay running while the green camera dot is off, so a process check turns the light on and never sees it turn off. Current macOS (Sequoia 15 and Tahoe 26) publishes `Frame publisher cameras changed to [app: …]` while the camera is held and `changed to [:]` when it is released. Sonoma also logs Control Center `cam:` / `mic:` attributions (microphone-only sessions stay off) and, on older builds, hardware power and `kCameraStream` lines. |
| Camera detection (Windows) | Polls the webcam privacy registry (`CapabilityAccessManager`) every 2 seconds — the same signal as the OS camera indicator |
| Light discovery | mDNS/Bonjour (`_elg._tcp.local.`). IPv4 is preferred |
| Light control | `PUT http://<light-ip>:9123/elgato/lights` with `{"on": 0}` or `{"on": 1}` only, so brightness and temperature stay as you set them |

Install runs one sync to that camera state. It does not blink the light on.

## Check detection without changing the light

Replay captured macOS log lines (works on any OS):

```bash
python3 main.py --sample-log tests/fixtures/macos-camera.log
```

The fixture includes a macOS 26.5 Zoom "camera on" line, the macOS 26 empty-dictionary "camera off" line `[:]`, Sonoma/Sequoia `cam:` and `mic:` attributions, and a hardware power-on echo that must not turn the light back on.

On a Mac, follow the live camera and print what the light would do:

```bash
python3 main.py --dry-run
```

State-machine tests:

```bash
python3 -m unittest discover -s tests -v
```

## Manual usage

```bash
python3 main.py --sync     # set the light once to match the camera, then exit
python3 main.py            # run in the foreground
```

`--test` is the same as `--sync`. It does not toggle the light on and off.

## Notes

- If the light is unplugged, the service keeps looking for it and applies the current camera state when it reappears.
- A missing camera log means **off**. The light is not turned on just because the service started.

## License

MIT
