# Archive Helper for Jellyfin

![Archive Helper GUI main window (latest)](screenshots/archive-helper-main.png)

Archive Helper is a small Python app that helps you copy DVDs/Blu-rays on a Linux “rip server”, convert them to a Jellyfin-friendly format, and put them into the right folders so Jellyfin can automatically scan and organize your library.

If you are brand new to Linux and Python: this project is meant to reduce how much terminal work you need to do. You still need a Linux server with the right tools installed, but day-to-day ripping is driven from a simple desktop GUI.

## What this app is for

- You have a Linux machine (physical or virtual) with an optical drive attached. This is your rip server.
- You want your finished files to land in a Movies/Series folder that Jellyfin watches.
- You want a “remote control” app that connects over SSH, starts a rip/encode workflow, shows progress, and prompts you when to swap discs.

## Rip modes (remote vs local)

The GUI supports choosing where ripping and encoding happen.

Three modes are available:

- **Rip + encode on server (remote)** (default): Everything happens on the server. The GUI controls the server over SSH and shows progress.
- **Rip locally, encode on server**: Your desktop rips the disc. The app uploads the raw MKV files to the server, then the server encodes them to MP4 or MKV (configurable).
- **Rip + encode locally, upload results** (beta): Your desktop does all the work, then uploads final encoded files to the server.

If you do not pick a mode, the app uses **Rip + encode on server (remote)**.

Note: The first two modes are stable. The third mode (local rip + encode upload) is now available as **beta**.

## Recent improvements (January 2026)

- **TMDB-assisted title matching**: In manual schedule mode, the GUI can probe the inserted disc on the server (volume/title hints via Linux CLI tools), query TMDB automatically, and present suggestions. Selecting a suggestion fills type/title/year; a built-in **No match** option keeps manual entry available.
- **TMDB API key setting**: You can store your TMDB API key in Settings → Connection so lookups are one click.
- **Local destination setting**: When using local rip mode, you can now set where temporary files are stored on your desktop. Go to Settings → Directories to configure this.
- **Disk space checks**: The app now checks that you have enough free space before ripping each disc (default: 20 GB). If space is low, it pauses and prompts you to free up space before continuing.
- **Better overlap mode**: When encoding a disc while ripping the next one, HandBrake no longer gets confused by keypresses. This means encodes keep running smoothly while you work on the next disc.
- **Faster DVD ripping**: MakeMKV now uses more cache memory (512 MB instead of 128 MB) when ripping DVDs, which makes rips more stable and can reduce errors.

## How it works (two-computer model)

This project has two scripts:

- `rip_and_encode_gui.py` (runs on your desktop): the Tkinter GUI.
- `rip_and_encode.py` (runs on the server): the rip/encode workflow the GUI uploads and starts remotely.

The rip server does the heavy work with:

- MakeMKV (reads the disc to MKV)
- HandBrakeCLI (encodes/transcodes)
- GNU screen (keeps the job running even if you close the GUI)

The GUI connects to the server using SSH. It can use a password, but SSH keys are strongly recommended.

Note: **Rip + encode locally, upload results** is currently beta. The other two modes are stable.

## What you need

### On your desktop (where you run the GUI)

- Python 3
- Tkinter for Python (often packaged as `python3-tk` on Debian/Ubuntu)
- Python packages: `paramiko` and `keyring`
- MakeMKV (required for local ripping modes)
- HandBrakeCLI (required for **Rip + encode locally, upload results** mode)
- At least 20 GB free disk space (for local ripping modes; configurable in Settings)
- Network access to your server’s SSH port

### On your rip server (Debian is a good choice)

- Debian (or another Linux distribution) with SSH enabled
- Python 3
- GNU screen
- MakeMKV
- HandBrakeCLI
- Enough free disk space for temporary rips and final output

## Installation and running (beginner-friendly)

The steps below assume:

- Desktop and server are on the same network (or reachable over the internet).
- Your server username is something like `jellyfin`.
- Your SSH port is the default `22`.

### Step 1: Download this project on your desktop

Open a terminal on your desktop and run:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-tk

git clone https://github.com/shunobies/ArchiveHelper.git
cd ArchiveHelper
```

If you downloaded a ZIP instead of using `git clone`, extract it and `cd` into the folder.

### Step 2: Create a Python virtual environment and install dependencies

From inside the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate

python3 -m pip install --upgrade pip
python3 -m pip install paramiko keyring
```

### Step 3: Set up your Debian rip server with SSH

At minimum, your server needs SSH enabled and reachable.

If you are new to Debian server setup, these guides are good starting points:

- Debian documentation: https://www.debian.org/doc/
- The Debian Administrator’s Handbook (free online book): https://debian-handbook.info/

Install and enable SSH on the server:

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
```

### Step 4 (recommended): Use SSH keys instead of passwords

Using SSH keys is safer and more reliable than typing a password. A simple, beginner-friendly flow:

1) Create a key on your desktop:

```bash
ssh-keygen -t ed25519
```

2) Copy your key to the server (replace `USER` and `HOST`):

```bash
ssh-copy-id USER@HOST
```

3) Test login:

```bash
ssh USER@HOST
```

Good learning resources:

- SSH key basics (SSH Academy): https://www.ssh.com/academy/ssh/keygen
- `ssh-copy-id` usage (OpenSSH): https://man.openbsd.org/ssh-copy-id

If you want to harden SSH by disabling password logins, only do that after confirming keys work:

- OpenSSH server config: https://man.openbsd.org/sshd_config

### Step 5: Install Jellyfin and set up libraries

Jellyfin setup differs by OS and preference. The official docs are the best starting point:

- Jellyfin documentation: https://jellyfin.org/docs/
- Jellyfin install guides: https://jellyfin.org/docs/general/installation/

In Jellyfin, create (or confirm) library folders such as:

- Movies directory (example): `/storage/Movies`
- Series directory (example): `/storage/Series`

Those paths must exist on the server and be writable by the user you connect as.

### Step 6: Install MakeMKV and HandBrakeCLI on the server

These tools are installed on the rip server (not your desktop).

- MakeMKV: https://www.makemkv.com/
- HandBrakeCLI: https://handbrake.fr/

MakeMKV is not always in Debian’s default repositories; follow the official instructions for your environment.

### Step 7: Run the GUI

On your desktop, from the project folder:

```bash
source .venv/bin/activate
python3 ./rip_and_encode_gui.py
```

### Optional: make GUI launch with a double-click icon

Yes—this is achievable, and your Python choice is good for cross-platform support.

The easiest options are:

- **Linux desktop launcher (`.desktop`)**: create a launcher icon that runs your virtual environment Python with `rip_and_encode_gui.py`.
- **Windows shortcut (`.lnk`)**: point a shortcut at `pythonw.exe` (or a packaged `.exe`) and pass `rip_and_encode_gui.py`.
- **macOS app bundle**: package with a tool like `py2app`/`Briefcase` so users launch like a normal app.

For non-technical users, a packaged installer is usually best:

- **PyInstaller** can build a standalone executable for each platform.
- Build per-OS (Windows build on Windows, macOS on macOS, Linux on Linux).
- You can then distribute a zip/installer and users just double-click to run.

Example Linux `.desktop` file (adjust paths):

```ini
[Desktop Entry]
Type=Application
Name=Archive Helper
Comment=Launch Archive Helper GUI
Exec=/home/YOURUSER/ArchiveHelper/.venv/bin/python /home/YOURUSER/ArchiveHelper/rip_and_encode_gui.py
Path=/home/YOURUSER/ArchiveHelper
Terminal=false
Categories=Utility;
```

Save as `~/.local/share/applications/archive-helper.desktop`, then run:

```bash
chmod +x ~/.local/share/applications/archive-helper.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

After that, Archive Helper appears in your app menu and can be pinned to the dock/taskbar.

If you prefer copy/paste templates, starter launchers are included in `launchers/`:

- `launchers/linux.desktop`
- `launchers/build_linux_exe.sh`
- `launchers/build_windows_exe.cmd` (recommended on Windows 11; avoids PowerShell execution policy issues)
- `launchers/build_windows_exe.ps1`
- `launchers/build_macos_app.sh`
- `launchers/macos.command`

Build-style launchers:

- Linux: run `./launchers/build_linux_exe.sh` to produce `dist/ArchiveHelper`.
- Windows (recommended): run `launchers\build_windows_exe.cmd` from Command Prompt to produce `dist\ArchiveHelper.exe`.
- Windows (PowerShell alternative): run `launchers/build_windows_exe.ps1` from PowerShell. Note that many Windows 11 systems block `.ps1` scripts by default unless execution policy is adjusted or bypassed for the command.
- macOS: run `./launchers/build_macos_app.sh` to produce `dist/ArchiveHelper.app`.

On first run (or if settings are missing), the app will prompt you to configure:

- Connection (SSH host/user/key/password)
- Output directories (Movies/Series and other placeholders)

After that, you can load a CSV schedule and press Start, or use manual mode.

Tip: in manual mode, click **Scan Disc + TMDB** first. The server will inspect the inserted disc and return TMDB suggestions. Choose a match to auto-fill type/title/year, or choose **No match** and enter details manually.

## Common questions

### Where do the finished files go?

The finished files are written on the rip server, into the Movies/Series directories you set in Settings. Jellyfin should be configured to scan those directories.

### What happens if I close the GUI while it is ripping?

The job runs on the server inside a `screen` session. Closing the GUI does not necessarily stop the job. When you reopen the GUI, it can offer to reattach and continue showing progress.

## Troubleshooting

- **Can't connect over SSH**: verify `ssh USER@HOST` works from the desktop first.
- **Preset list is empty**: confirm `HandBrakeCLI --preset-list` works on the server.
- **Permission errors writing to Movies/Series folders**: verify the SSH user can write to those directories.
- **HandBrake seems to pause when inserting next disc**: this was fixed in recent versions. The app now prevents disc prompts from affecting background encodes. Update to the latest version.
- **Local mode says "not enough disk space"**: the app checks that you have at least 20 GB free before ripping each disc. Free up space or change the threshold in Settings.

## License

This repository does not currently include a license file. If you plan to redistribute or publish it, add an explicit license first.


### Subtitle behavior for Jellyfin

- Default behavior uses `--output-container mp4` and `--subtitle-mode external` so files stay in the existing MP4 workflow while extracting subtitles from source MKVs into Jellyfin-readable sidecar files.
- You can change this with:
  - `--output-container mp4|mkv`
  - `--subtitle-mode preset|soft|external|none`

For external subtitle mode, sidecar names are generated beside the video using two-letter language tags, for example `MovieName.en.srt`.

For DVD/BD image-based subtitles, MKV is recommended for best soft-subtitle compatibility in Jellyfin.
