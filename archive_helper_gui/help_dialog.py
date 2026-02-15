import webbrowser


def show_help_dialog(root) -> None:
    # Use a scrollable window (smaller font) instead of a tall messagebox.
    import tkinter as tk
    from tkinter import BOTH, LEFT, RIGHT, X, ttk

    win = tk.Toplevel(root)
    win.title("Help")
    win.transient(root)
    try:
        win.grab_set()
    except Exception:
        pass

    # Size: keep smaller than the main window, but reasonable by default.
    try:
        root.update_idletasks()
        mw = max(600, int(root.winfo_width() * 0.85))
        mh = max(420, int(root.winfo_height() * 0.85))
        w = min(760, mw)
        h = min(560, mh)
    except Exception:
        w, h = 720, 520

    win.geometry(f"{w}x{h}")

    container = ttk.Frame(win, padding=10)
    container.pack(fill=BOTH, expand=True)

    ttk.Label(container, text="Archive Helper for Jellyfin", font=("TkDefaultFont", 12, "bold")).pack(
        anchor="w", pady=(0, 6)
    )

    text = tk.Text(
        container,
        wrap="word",
        font=("TkDefaultFont", 9),
        height=1,
        borderwidth=1,
        relief="solid",
    )
    scroll = ttk.Scrollbar(container, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)

    scroll.pack(side=RIGHT, fill="y")
    text.pack(side=LEFT, fill=BOTH, expand=True)

    # Styling tags (keep default colors).
    text.tag_configure("h1", font=("TkDefaultFont", 11, "bold"), spacing1=10, spacing3=6)
    text.tag_configure("h2", font=("TkDefaultFont", 10, "bold"), spacing1=8, spacing3=2)
    text.tag_configure("p", font=("TkDefaultFont", 9), spacing1=0, spacing3=6)
    text.tag_configure("num", font=("TkDefaultFont", 9, "bold"), lmargin1=0, lmargin2=0, spacing1=2, spacing3=2)
    text.tag_configure(
        "bullet",
        font=("TkDefaultFont", 9),
        lmargin1=18,
        lmargin2=36,
        spacing1=1,
        spacing3=1,
    )
    text.tag_configure(
        "subbullet",
        font=("TkDefaultFont", 9),
        lmargin1=36,
        lmargin2=54,
        spacing1=1,
        spacing3=1,
    )
    text.tag_configure("example", font=("TkDefaultFont", 9), lmargin1=36, lmargin2=36, spacing1=0, spacing3=4)
    text.tag_configure("link", foreground="#1a0dab", underline=True)

    def add_line(s: str, tag: str = "p") -> None:
        text.insert("end", s + "\n", tag)

    def add_blank() -> None:
        text.insert("end", "\n")

    def add_bullets(items: list[str], indent: str = "bullet") -> None:
        for it in items:
            add_line("• " + it, indent)

    def add_link(label: str, url: str, *, indent_tag: str = "p") -> None:
        label_s = (label or "").strip()
        url_s = (url or "").strip()
        if not label_s or not url_s:
            return

        tag = f"link_{abs(hash(url_s))}"
        text.tag_configure(tag, foreground="#1a0dab", underline=True)

        def _open(_event=None) -> None:
            try:
                webbrowser.open(url_s)
            except Exception:
                pass

        try:
            text.tag_bind(tag, "<Button-1>", _open)
            text.tag_bind(tag, "<Enter>", lambda _e: text.configure(cursor="hand2"))
            text.tag_bind(tag, "<Leave>", lambda _e: text.configure(cursor=""))
        except Exception:
            pass

        text.insert("end", label_s + "\n", (indent_tag, tag))

    # Content (structured for readable formatting)
    add_line("Overview: What is this app and why does it exist?", "h1")
    add_blank()
    add_line(
        "Archive Helper for Jellyfin is a helper tool that makes it easier to rip DVDs or Blu-rays on a Linux server and "
        "organize them so Jellyfin can automatically recognize and display them.",
        "p",
    )
    add_blank()
    add_line(
        "Instead of manually logging into a server, running commands, and watching terminal output, this app:",
        "p",
    )
    add_blank()
    add_bullets(
        [
            "Connects to your rip server for you",
            "Uploads the scripts it needs",
            "Starts the ripping and encoding process",
            "Shows you progress",
            "Tells you when it's time to change discs",
        ]
    )
    add_blank()
    add_line("Think of it as a remote control for a dedicated ripping machine.", "p")

    add_blank()
    add_line("What this app does (step by step)", "h1")
    add_blank()

    add_line("1. Connects to your rip server using SSH", "num")
    add_bullets(
        [
            "SSH is a secure way to remotely control another computer",
            "This app uses it so you don't have to open a terminal yourself",
        ],
        "subbullet",
    )
    add_blank()

    add_line("2. Uploads a schedule", "num")
    add_bullets(
        [
            "The schedule tells the server what you are ripping (movie or TV series)",
            "It can be entered manually or loaded from a CSV file",
        ],
        "subbullet",
    )
    add_blank()

    add_line("3. Uploads or updates the rip script", "num")
    add_bullets(
        [
            "This is the script that runs MakeMKV and HandBrake on the server",
            "If you already used this app before, it keeps it up to date automatically",
        ],
        "subbullet",
    )
    add_blank()

    add_line("4. Runs the ripping and encoding workflow", "num")
    add_bullets(
        [
            "MakeMKV copies the disc to the server",
            "HandBrake converts the video into a Jellyfin-friendly format",
        ],
        "subbullet",
    )
    add_blank()

    add_line("5. Shows progress and disc swap prompts", "num")
    add_bullets(
        [
            "You'll see what step it's on",
            "When multiple discs are needed, the app tells you when to insert the next one",
        ],
        "subbullet",
    )

    add_blank()
    add_line("Connection (SSH)", "h1")
    add_blank()
    add_line("This section tells the app how to reach your rip server.", "p")

    add_blank()
    add_line("Host", "h2")
    add_line("The IP address or hostname of the rip server", "p")
    add_line("Examples:", "p")
    add_line("192.168.1.10", "example")
    add_line("media-server.local", "example")
    add_line("This is the machine where the disc drive and ripping software live", "p")

    add_blank()
    add_line("User", "h2")
    add_line("The Linux username on the rip server", "p")
    add_line("This is usually the same name you use when logging into the server directly", "p")

    add_blank()
    add_line("Port", "h2")
    add_line("The SSH port used by the server", "p")
    add_line("Almost always 22", "p")
    add_line("You usually don't need to change this", "p")

    add_blank()
    add_line("Key file (optional)", "h2")
    add_line("An SSH private key file", "p")
    add_line("Allows logging in without typing a password", "p")
    add_line("Recommended for advanced users, but not required", "p")

    add_blank()
    add_line("Password", "h2")
    add_line("Required only if you are not using a key file", "p")
    add_line("This is the password for the Linux user above", "p")
    add_line("The app does not display or store it in plain text", "p")

    add_blank()
    add_line("Run settings", "h1")
    add_blank()
    add_line("These settings control where files go and how they are encoded.", "p")

    add_blank()
    add_line("Install Jellyfin if missing", "h2")
    add_line("If checked, the app will try to:", "p")
    add_bullets(["Install Jellyfin", "Enable and start the Jellyfin service"], "bullet")
    add_blank()
    add_line("This requires the server user to have sudo (administrator) access", "p")
    add_line("Safe to leave unchecked if Jellyfin is already installed", "p")

    add_blank()
    add_line("Movies dir", "h2")
    add_line("Folder on the server where movies will be saved", "p")
    add_line("Example:", "p")
    add_line("/storage/Movies", "example")
    add_line("Jellyfin should already be configured to scan this folder", "p")

    add_blank()
    add_line("Series dir", "h2")
    add_line("Folder on the server where TV series will be saved", "p")
    add_line("Example:", "p")
    add_line("/storage/Series", "example")

    add_blank()
    add_line("HandBrake preset", "h2")
    add_line("The name of the HandBrake preset used for encoding", "p")
    add_line("This must exactly match a preset available on the server", "p")
    add_line("Example:", "p")
    add_line("HQ 1080p30 Surround", "example")
    add_line("You can change this later without reinstalling anything", "p")

    add_blank()
    add_line("Output", "h2")
    add_line("Container for encoded files", "p")
    add_bullets(["mp4 (default): most compatible with existing workflow", "mkv: can preserve more subtitle formats"], "bullet")

    add_blank()
    add_line("Subtitles", "h2")
    add_line("How subtitle tracks are handled during encode", "p")
    add_bullets([
        "preset: use the subtitle rules already defined in the HandBrake preset",
        "soft: keep subtitle tracks selectable in Jellyfin (not burned in)",
        "external: extract subtitles from MKV with ffmpeg into sidecar files for Jellyfin",
        "none: remove subtitle tracks from encoded output",
    ], "bullet")

    add_blank()
    add_line("Schedule", "h1")
    add_blank()
    add_line("The schedule tells the app what you want to rip.", "p")
    add_line("You can use Manual mode or a CSV file.", "p")

    add_blank()
    add_line("Manual Schedule", "h2")
    add_line("Use this if you are ripping one movie or one season at a time.", "p")

    add_blank()
    add_line("Type", "h2")
    add_line("Choose:", "p")
    add_bullets(["Movie → single film", "Series → TV show"], "bullet")

    add_blank()
    add_line("Title", "h2")
    add_line("The name of the movie or TV series", "p")
    add_line("This is used to name folders and files", "p")
    add_line("Example:", "p")
    add_line("The Matrix", "example")

    add_blank()
    add_line("Year", "h2")
    add_line("Release year of the movie or series", "p")
    add_line("Helps Jellyfin match the correct metadata", "p")
    add_line("Example:", "p")
    add_line("1999", "example")

    add_blank()
    add_line("Season (series only)", "h2")
    add_line("Season number for TV series", "p")
    add_line("Example:", "p")
    add_line("1", "example")

    add_blank()
    add_line("Total discs", "h2")
    add_line("How many discs are part of this job", "p")
    add_line("Examples:", "p")
    add_bullets(["Movie with bonus disc → 2", "TV season with 4 DVDs → 4"], "bullet")
    add_line("The app will prompt you when it's time to insert the next disc", "p")

    add_blank()
    add_line("Current disc in drive", "h2")
    add_line("Which disc is currently in the drive when you press Start", "p")
    add_line("Use this if you are resuming in the middle of a multi-disc set", "p")

    add_blank()
    add_line("CSV Schedule", "h2")
    add_line("Use this if you want to:", "p")
    add_bullets(["Queue multiple movies or seasons", "Run unattended batches", "Reuse schedules later"], "bullet")
    add_blank()
    add_line("You select an existing CSV file, and the app will process each entry in order.", "p")

    add_blank()
    add_line("Buttons", "h1")
    add_blank()
    add_line("Start", "h2")
    add_bullets(
        [
            "Begins the ripping and encoding process",
            "Uploads scripts and schedule to the server",
            "Starts processing the first disc",
        ],
        "bullet",
    )
    add_blank()
    add_line("Continue", "h2")
    add_bullets(["Click this after inserting the next disc", "Used when ripping multiple discs in one job"], "bullet")
    add_blank()
    add_line("Stop", "h2")
    add_bullets(["Cancels the current job", "Safely stops processing on the server"], "bullet")
    add_blank()
    add_line("Show Log", "h2")
    add_bullets(
        [
            "Displays detailed output from the server",
            "Useful for troubleshooting or curiosity",
            "Safe to ignore if everything is working",
        ],
        "bullet",
    )

    add_blank()
    add_line("Additional notes (important)", "h1")
    add_blank()
    add_line("The rip server must have:", "p")
    add_bullets(["Python 3", "MakeMKV", "HandBrakeCLI"], "bullet")
    add_blank()
    add_line("These are not installed automatically unless you explicitly enable it", "p")
    add_blank()
    add_line("All output files are saved on the server, not on this computer", "p")
    add_line("This app remembers your settings between runs", "p")
    add_line("Advanced users can run the rip script directly on the server for full control", "p")

    add_blank()
    add_line("Credits", "h1")
    add_blank()
    add_line("Created by ChatGPT 5.2", "p")
    add_line("Conceptualized and designed by Alex Autrey", "p")
    add_blank()
    add_line("Websites (open-source and core dependencies)", "h2")
    add_link("Jellyfin: https://jellyfin.org/", "https://jellyfin.org/")
    add_link("MakeMKV: https://www.makemkv.com/", "https://www.makemkv.com/")
    add_link("Python: https://www.python.org/", "https://www.python.org/")
    add_link("HandBrake: https://handbrake.fr/", "https://handbrake.fr/")
    add_link("FFmpeg / ffprobe: https://ffmpeg.org/", "https://ffmpeg.org/")
    add_link("GNU Screen: https://www.gnu.org/software/screen/", "https://www.gnu.org/software/screen/")
    add_link("OpenSSH: https://www.openssh.com/", "https://www.openssh.com/")
    add_link("Paramiko (Python SSH library): https://www.paramiko.org/", "https://www.paramiko.org/")
    add_link("keyring (Python): https://pypi.org/project/keyring/", "https://pypi.org/project/keyring/")

    text.configure(state="disabled")

    btns = ttk.Frame(win, padding=(10, 0, 10, 10))
    btns.pack(fill=X)
    ttk.Button(btns, text="Close", command=win.destroy).pack(side=RIGHT)
