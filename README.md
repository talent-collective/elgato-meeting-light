# elgato-meeting-light

Turns an Elgato Key Light on when your camera is in use and off when it is not.

Works on **macOS** and **Windows**. The light is discovered on the local network. Brightness and color temperature are left alone.

## Install or reinstall on a Mac

Fresh install:

```bash
git clone https://github.com/talent-collective/elgato-meeting-light.git
cd elgato-meeting-light
bash setup.sh
```

Install without cloning first. The script clones the repo into `~/.elgato-meeting-light`, or into `$ELGATO_MEETING_LIGHT_HOME` if that is set. If that folder is already a checkout, it fast-forwards it, then runs `setup.sh` from there:

```bash
curl -fsSL https://raw.githubusercontent.com/talent-collective/elgato-meeting-light/master/setup.sh | bash
```

Update or reinstall. This works even if the checkout is still on an old feature branch, or has local edits. A plain `git pull` would stay on that branch. This command stashes local edits, switches to `master`, fast-forwards to `origin/master`, and runs setup. You can run it from any directory; `plutil` finds the install folder:

```bash
cd "$(plutil -extract WorkingDirectory raw ~/Library/LaunchAgents/com.elgato-meeting-light.plist)" && git stash push && git fetch origin master && git checkout master && git merge --ff-only origin/master && bash setup.sh
```

Running `setup.sh` again is safe. It installs dependencies first, then replaces the LaunchAgent, blinks the light on then off to check the connection, and leaves the light matched to the camera.

| | |
|---|---|
| LaunchAgent | `com.elgato-meeting-light` |
| Plist | `~/Library/LaunchAgents/com.elgato-meeting-light.plist` |

The agent loads at login (`RunAtLoad`) and restarts if it exits (`KeepAlive`). It runs the checkout's `.venv` Python, so launchd does not depend on your shell `PATH`.

### macOS permissions

Allow **Local Network** if macOS prompts, so Python can reach the Key Light at `http://<light>:9123/elgato/lights`. Camera access is not required. This program does not open the camera.

Elgato Control Center needs to be running so the light is advertised on the network.

## Uninstall

Setup prints an uninstall command with the absolute path, so it works from any directory:

```bash
bash /path/to/setup.sh --uninstall
```

Uninstall stops the LaunchAgent and deletes `~/Library/LaunchAgents/com.elgato-meeting-light.plist`. The checkout, the `.venv`, and the log files stay where they are.

If the camera is on when you uninstall, the light can stay on. Uninstall does not send an off command.

## Logs

The log files are in the install folder, which is the LaunchAgent working directory:

```bash
plutil -extract WorkingDirectory raw ~/Library/LaunchAgents/com.elgato-meeting-light.plist
```

| File | Contents |
|---|---|
| `elgato-light.log` | Application log |
| `elgato-light.launchd.out.log` | launchd standard output |
| `elgato-light.launchd.err.log` | launchd standard error, including startup errors |

Nothing rotates or truncates these files.

```bash
cd "$(plutil -extract WorkingDirectory raw ~/Library/LaunchAgents/com.elgato-meeting-light.plist)"
tail -f elgato-light.log
```

Lines look like `Initial camera state: off`, `Camera state changed: off -> on`, and `Light ON` / `Light OFF`.

`--probe` prints to the terminal only. It does not append to these files.

Check the live camera reading for 20 seconds (does not change the light):

```bash
.venv/bin/python main.py --probe
```

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

`--dry-run` logs the camera state and the light action it would take (`Dry run: would turn light ON` / `OFF`). It sends no command to the light, and it keeps running until you stop it:

```bash
.venv/bin/python main.py --dry-run
```

## Auto-off

If the camera stays in use for more than four hours, the light turns off and stays off, even while the camera is still reported in use. The log line is:

```text
Light on for over 4h continuously; auto-off. Will turn on again after the camera goes off and back on.
```

One camera-off reading re-arms it. The next time the camera comes on, the light turns on and a new four-hour window starts.

- A restart of the watcher while the light is latched off turns the light back on. That includes a reboot, logging in, a reinstall, and KeepAlive starting the process again. The latch is only in memory.
- Time the computer spends asleep counts. The timer uses wall-clock time.
- Four hours is the fixed constant `AUTO_OFF_AFTER_SECONDS` in `auto_off.py`. It is not a setting.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
