# elgato-meeting-light

Turns an Elgato Key Light on when your camera is in use and off when it is not.

Works on **macOS** and **Windows**. The light is discovered on the local network. Brightness and color temperature are left alone.

## Install or reinstall on a Mac

From a checkout of this repo:

```bash
bash setup.sh
```

Running that again is safe. It stops the existing LaunchAgent, replaces it, blinks the light on then off to check the connection, and leaves the light matched to the camera.

If you already have the repo at `~/elgato-meeting-light/elgato-meeting-light`:

```bash
cd ~/elgato-meeting-light/elgato-meeting-light && git fetch origin cursor/fix-macos-camera-detection-ad96 && git checkout -B cursor/fix-macos-camera-detection-ad96 origin/cursor/fix-macos-camera-detection-ad96 && bash setup.sh
```

Uninstall: `bash setup.sh --uninstall`

| | |
|---|---|
| LaunchAgent | `com.elgato-meeting-light` |
| Plist | `~/Library/LaunchAgents/com.elgato-meeting-light.plist` |
| Log | `elgato-light.log` in the project directory |
| Startup errors | `elgato-light.launchd.err.log` in the project directory |

The agent loads at login (`RunAtLoad`) and restarts if it exits (`KeepAlive`). It runs the checkout's `.venv` Python, so launchd does not depend on your shell `PATH`.

Watch it:

```bash
tail -f elgato-light.log
```

Lines look like `Initial camera state: off`, `Camera state changed: off -> on`, and `Light ON` / `Light OFF`.

Check the live camera reading for 20 seconds (does not change the light):

```bash
.venv/bin/python main.py --probe
```

### macOS permissions

Allow **Local Network** if macOS prompts, so Python can reach the Key Light at `http://<light>:9123/elgato/lights`. Camera access is not required. This program does not open the camera.

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
| Camera detection (macOS) | CoreMediaIO `kCMIODevicePropertyDeviceIsRunningSomewhere` (`gone`) on every video device from `kCMIOHardwarePropertyDevices`. `VDCAssistant` and `cameracaptured` are persistent daemons on macOS 26, so a process check stays true while the camera is off. If the CoreMediaIO call fails, the camera is treated as off. |
| Camera detection (Windows) | Polls the webcam privacy registry (`CapabilityAccessManager`) every 2 seconds |
| Light discovery | mDNS/Bonjour (`_elg._tcp.local.`). IPv4 is preferred |
| Light control | `PUT http://<light-ip>:9123/elgato/lights` with only `on` set, so brightness and temperature stay as you set them |

`--test` (used by install) turns the light on, then off, then sets it to the real camera state. It does not leave the light on when the camera is off.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
