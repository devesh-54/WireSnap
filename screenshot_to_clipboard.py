"""
Phone screenshot -> Windows clipboard (Android over USB).

 - Screenshot taken on the phone  -> copied to clipboard automatically
 - Hotkey Ctrl+Alt+P on laptop    -> captures phone screen, copies to clipboard
 - A soft white glow appears around your mouse cursor when the clipboard is ready
 - Images in "Pictures\\WireSnap" are deleted after 7 days

Requires: Python 3 (with tkinter), adb, USB debugging on, and:
    pip install pillow pystray
(adb: uses a 'platform-tools' folder next to this script/exe if present,
 otherwise adb from PATH.)
Run:  python screenshot_to_clipboard.py      (or pythonw for no console)
"""
import ctypes
import io
import math
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from ctypes import wintypes

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
except ImportError:
    print("Missing package. Run:  pip install pillow")
    raise SystemExit(1)

try:
    import pystray
    PYSTRAY_ERROR = None
except Exception as e:  # tray icon is skipped if pystray cannot load
    pystray = None
    PYSTRAY_ERROR = repr(e)

# ---------- settings ----------
SAVE_DIR = os.path.join(os.path.expanduser("~"), "Pictures", "WireSnap")
KEEP_DAYS = 7
POLL_SECONDS = 0.5
ENABLE_HOTKEY = True          # set False to turn the hotkey off
HOTKEY_KEY = "P"              # used with Ctrl+Alt
GLOW_SECONDS = 1.2
GLOW_COLOR = (255, 255, 255)  # white; e.g. (80, 160, 255) for blue
SCREENSHOT_DIRS = [
    "/sdcard/Pictures/Screenshots",
    "/sdcard/DCIM/Screenshots",
    "/sdcard/Pictures/Screenshot",
    "/sdcard/DCIM/Screenshot",
]
# ------------------------------

try:  # sharp glow on scaled (125%/150%) displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

os.makedirs(SAVE_DIR, exist_ok=True)
APP_NAME = "WireSnap"
paused = False          # toggled from the tray menu (always starts unpaused)
phone_connected = False  # updated by the watcher
tray_icon = None


def find_adb():
    here = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    for base in (here, getattr(sys, "_MEIPASS", here)):
        c = os.path.join(base, "platform-tools", "adb.exe")
        if os.path.exists(c):
            return c
    return "adb"  # fall back to adb in PATH


ADB = find_adb()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
glow_queue = queue.Queue()
last_seen = {}  # serial -> newest filename already handled
LOG_FILE = os.path.join(SAVE_DIR, "screenshot_tool.log")


def message_box(text, title="WireSnap"):
    """Popup so errors are visible even without a console window."""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        pass


def log_error():
    import traceback
    try:
        with open(LOG_FILE, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")
    except OSError:
        pass


# ---------- adb helpers ----------
def run(args):
    return subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, text=True,
                          creationflags=NO_WINDOW)


def connected_devices():
    lines = run([ADB, "devices"]).stdout.splitlines()[1:]
    return [l.split()[0] for l in lines if l.strip().endswith("\tdevice")]


def newest_screenshot(serial):
    for folder in SCREENSHOT_DIRS:
        cmd = f"ls -t {shlex.quote(folder)} 2>/dev/null | head -n 1"
        name = run([ADB, "-s", serial, "shell", cmd]).stdout.strip()
        if name:
            return folder, name
    return None


def pull(serial, folder, name):
    local = os.path.join(SAVE_DIR, f"{serial}_{name}")
    for i in range(8):
        if i:
            time.sleep(0.25)  # only wait if the phone is still writing the file
        run([ADB, "-s", serial, "pull", f"{folder}/{name}", local])
        if os.path.exists(local) and os.path.getsize(local) > 0:
            try:
                with Image.open(local) as im:
                    im.load()  # fails on half-written files -> retry
                return local
            except Exception:
                pass
    return None


# ---------- clipboard ----------
def copy_via_powershell(path):  # slower fallback
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$i=[System.Drawing.Image]::FromFile($env:SHOT_PATH);"
        "$b=New-Object System.Drawing.Bitmap($i);$i.Dispose();"
        "[System.Windows.Forms.Clipboard]::SetImage($b)"
    )
    env = {**os.environ, "SHOT_PATH": path}
    subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                   env=env, capture_output=True, stdin=subprocess.DEVNULL,
                   creationflags=NO_WINDOW)


_kernel32 = ctypes.windll.kernel32
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]


def copy_via_win32(path):
    """Put the image on the clipboard directly (no PowerShell start-up delay)."""
    with Image.open(path) as img:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]  # drop the 14-byte BMP file header -> raw DIB
    h = _kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
    if not h:
        return False
    p = _kernel32.GlobalLock(h)
    if not p:
        _kernel32.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    _kernel32.GlobalUnlock(h)
    for _ in range(10):  # the clipboard can be busy for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        _kernel32.GlobalFree(h)
        return False
    try:
        user32.EmptyClipboard()
        ok = user32.SetClipboardData(8, h)  # 8 = CF_DIB
    finally:
        user32.CloseClipboard()
    if not ok:
        _kernel32.GlobalFree(h)
        return False
    return True


def copy_to_clipboard(path):
    try:
        if copy_via_win32(path):
            return
    except Exception:
        log_error()
    copy_via_powershell(path)  # fallback so a copy never silently fails


def done(path):
    copy_to_clipboard(path)
    glow_queue.put(1)


# ---------- cleanup ----------
def cleanup_old():
    cutoff = time.time() - KEEP_DAYS * 86400
    for f in os.listdir(SAVE_DIR):
        p = os.path.join(SAVE_DIR, f)
        try:
            if f.lower().endswith(".png") and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


# ---------- watcher thread (phone screenshot button) ----------
def watcher():
    last_cleanup = 0
    while True:
        try:
            if time.time() - last_cleanup > 3600:
                cleanup_old()
                last_cleanup = time.time()
            devices = connected_devices()
            set_phone_state(bool(devices))
            for serial in devices:
                found = newest_screenshot(serial)
                if not found:
                    continue
                folder, name = found
                if serial not in last_seen:   # first contact: baseline only
                    last_seen[serial] = name
                    continue
                if name != last_seen[serial]:
                    last_seen[serial] = name
                    if paused:
                        continue
                    local = pull(serial, folder, name)
                    if local:
                        done(local)
        except Exception:
            log_error()
        time.sleep(POLL_SECONDS)


# ---------- hotkey thread (laptop captures phone screen) ----------
def capture_now():
    devices = connected_devices()
    if not devices:
        return
    serial = devices[0]
    r = subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                       capture_output=True, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    if len(r.stdout) < 1000:
        return
    path = os.path.join(SAVE_DIR, f"{serial}_capture_{int(time.time())}.png")
    with open(path, "wb") as f:
        f.write(r.stdout)
    done(path)


def hotkey_loop():
    MOD_CTRL_ALT_NOREPEAT = 0x0002 | 0x0001 | 0x4000
    if not user32.RegisterHotKey(None, 1, MOD_CTRL_ALT_NOREPEAT,
                                 ord(HOTKEY_KEY.upper())):
        print(f"Could not register Ctrl+Alt+{HOTKEY_KEY} (already in use?)")
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            try:
                capture_now()
            except Exception:
                log_error()


# ---------- soft glow around the mouse cursor ----------
# A per-pixel-alpha layered window (UpdateLayeredWindow) lets the glow blend
# smoothly with the screen. Pillow draws a blurred cursor-shaped halo.
ARROW = [(0, 0), (0, 16), (4, 12), (7, 19), (10, 18), (7, 11), (12, 11)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPels", wintypes.LONG),
                ("biYPels", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


H = ctypes.c_void_p
user32.CreateWindowExW.restype = H
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, H, H, H, H]
user32.GetDC.restype = H
user32.GetDC.argtypes = [H]
user32.ShowWindow.argtypes = [H, ctypes.c_int]
user32.UpdateLayeredWindow.argtypes = [H, H, ctypes.c_void_p, ctypes.c_void_p,
                                       H, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD]
gdi32.CreateCompatibleDC.restype = H
gdi32.CreateCompatibleDC.argtypes = [H]
gdi32.CreateDIBSection.restype = H
gdi32.CreateDIBSection.argtypes = [H, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), H,
                                   wintypes.DWORD]
gdi32.SelectObject.restype = H
gdi32.SelectObject.argtypes = [H, H]


def dpi_scale():
    try:
        return user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


class Glow:
    def __init__(self, root):
        self.root = root
        d = dpi_scale()
        self.size = int(100 * d)
        self.pad = int(36 * d)       # cursor tip sits at (pad, pad)
        self.base = self._make_base(d)
        self.start = 0.0
        self.running = False

        # WS_EX_TOPMOST|TOOLWINDOW|TRANSPARENT(click-through)|LAYERED|NOACTIVATE
        ex = 0x8 | 0x80 | 0x20 | 0x80000 | 0x08000000
        self.hwnd = user32.CreateWindowExW(ex, "STATIC", None, 0x80000000,
                                           0, 0, self.size, self.size,
                                           None, None, None, None)
        if not self.hwnd:
            raise RuntimeError("could not create glow window")

        self.hdc_screen = user32.GetDC(None)
        self.hdc_mem = gdi32.CreateCompatibleDC(self.hdc_screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), self.size,
                               -self.size, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(self.hdc_screen, ctypes.byref(bmi),
                                           0, ctypes.byref(bits), None, 0)
        gdi32.SelectObject(self.hdc_mem, self.hbmp)
        self.bits = bits
        self.blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, per-pixel alpha

    def _make_base(self, d):
        s, pad, sc = self.size, self.pad, 1.1 * d
        m = Image.new("L", (s, s), 0)
        ImageDraw.Draw(m).polygon(
            [(pad + x * sc, pad + y * sc) for x, y in ARROW], fill=255)
        m = m.filter(ImageFilter.MaxFilter(3))
        wide = m.filter(ImageFilter.GaussianBlur(12 * d)).point(
            lambda v: int(v * 0.9))
        med = m.filter(ImageFilter.GaussianBlur(5 * d)).point(
            lambda v: int(v * 0.9))
        tight = m.filter(ImageFilter.GaussianBlur(2 * d)).point(
            lambda v: int(v * 0.9))
        glow = ImageChops.add(ImageChops.add(wide, med), tight)
        # brighten the white without growing the shape
        return glow.point([min(255, int(255 * (v / 255) ** 0.75)) for v in range(256)])

    def _draw(self, k):
        lut = [min(255, int(v * k)) for v in range(256)]
        a = self.base.point(lut)
        r, g, b = GLOW_COLOR
        ch = lambda c: a.point([v * c // 255 for v in range(256)])
        data = Image.merge("RGBA", (ch(b), ch(g), ch(r), a)).tobytes()  # BGRA, premultiplied
        ctypes.memmove(self.bits, data, len(data))

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        dst = wintypes.POINT(pt.x - self.pad, pt.y - self.pad)
        size = wintypes.SIZE(self.size, self.size)
        src = wintypes.POINT(0, 0)
        user32.UpdateLayeredWindow(self.hwnd, self.hdc_screen,
                                   ctypes.byref(dst), ctypes.byref(size),
                                   self.hdc_mem, ctypes.byref(src), 0,
                                   ctypes.byref(self.blend), 2)  # ULW_ALPHA

    def show(self):
        self.start = time.time()
        if self.running:
            return
        self.running = True
        user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        self.tick()

    def tick(self):
        t = time.time() - self.start
        if t > GLOW_SECONDS:
            user32.ShowWindow(self.hwnd, 0)  # SW_HIDE
            self.running = False
            return
        ramp = min(1.0, t / 0.12)                      # quick fade-in
        fade = 1.0 if t < GLOW_SECONDS * 0.45 else (1 - (t - GLOW_SECONDS * 0.45) / (GLOW_SECONDS * 0.55)) ** 1.2  # hold, then fade
        pulse = 0.92 + 0.08 * math.sin(t * 14)
        self._draw(ramp * fade * pulse)
        self.root.after(16, self.tick)


# ---------- tray icon + start with Windows ----------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_parts():
    """(program, arguments) that start this tool."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return exe, f'"{os.path.abspath(__file__)}"'


def startup_command():
    target, args = launch_parts()
    return f'"{target}" {args}'.strip()


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enable):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ,
                                  startup_command())
            else:
                winreg.DeleteValue(k, APP_NAME)
    except OSError:
        pass


def ensure_start_menu_shortcut():
    """So the app can be reopened after Exit: Win key, type WireSnap, Enter."""
    lnk = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                       "Start Menu", "Programs", f"{APP_NAME}.lnk")
    if os.path.exists(lnk):
        return
    target, args = launch_parts()
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
          "$s.TargetPath=$env:TGT;$s.Arguments=$env:ARGS;"
          "$s.WorkingDirectory=$env:WD;$s.Save()")
    env = {**os.environ, "LNK": lnk, "TGT": target, "ARGS": args,
           "WD": os.path.dirname(target)}
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env,
                   capture_output=True, stdin=subprocess.DEVNULL,
                   creationflags=NO_WINDOW)


# only one copy may run (startup entry + manual launch would double up)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = ctypes.c_void_p
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_mutex = None


def already_running():
    global _mutex
    _mutex = _k32.CreateMutexW(None, False, "WireSnap_single_instance")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def status_text():
    if paused:
        return "Paused (auto-copy is off)"
    return "Phone connected - ready" if phone_connected else "Waiting for phone..."


def make_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(30, 34, 48, 255))
    dr.polygon([(12 + x * 2.0, 10 + y * 2.0) for x, y in ARROW],
               fill=(255, 255, 255, 255))
    # status dot: orange = paused, green = phone connected, grey = no phone
    dot = (255, 150, 0) if paused else (60, 220, 100) if phone_connected else (130, 130, 130)
    dr.ellipse((38, 38, 60, 60), fill=dot + (255,), outline=(30, 34, 48, 255), width=3)
    return img


def set_phone_state(connected):
    global phone_connected
    if connected != phone_connected:
        phone_connected = connected
        refresh_tray()


def refresh_tray():
    if tray_icon is None:
        return
    try:
        tray_icon.icon = make_tray_image()
        tray_icon.title = f"{APP_NAME}: {status_text()}"
        tray_icon.update_menu()
    except Exception:
        pass


def start_tray():
    global tray_icon
    if pystray is None:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(f"Tray icon unavailable: {PYSTRAY_ERROR}\n")
        except OSError:
            pass
        message_box("The tray icon could not start, so there is no tray menu.\n"
                    "WireSnap still works. To close it, end WireSnap.exe in Task Manager.\n\n"
                    f"Details: {PYSTRAY_ERROR}")
        return

    def toggle_pause(icon, item):
        global paused
        paused = not paused
        refresh_tray()

    def toggle_startup(icon, item):
        set_startup(not startup_enabled())

    def open_folder(icon, item):
        os.startfile(SAVE_DIR)

    def quit_app(icon, item):
        icon.stop()
        glow_queue.put("quit")

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: status_text(), lambda icon, item: None,
                         enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open WireSnap folder", open_folder),
        pystray.MenuItem(
            lambda item: "Resume auto-copy" if paused else "Pause auto-copy",
            toggle_pause),
        pystray.MenuItem("Start with Windows", toggle_startup,
                         checked=lambda item: startup_enabled()),
        pystray.MenuItem("Exit WireSnap", quit_app),
    )
    tray_icon = pystray.Icon(APP_NAME, make_tray_image(),
                             f"{APP_NAME}: {status_text()}", menu)
    tray_icon.run_detached()
    time.sleep(0.2)
    refresh_tray()


def main():
    if already_running():
        message_box("WireSnap is already running.\n"
                    "Look for its icon in the tray (the ^ arrow near the clock).")
        return
    threading.Thread(target=ensure_start_menu_shortcut, daemon=True).start()
    root = tk.Tk()
    root.withdraw()
    try:
        glow = Glow(root)
    except Exception as e:
        glow = None
        print("Glow disabled:", e)

    def poll_queue():
        try:
            while True:
                msg = glow_queue.get_nowait()
                if msg == "quit":
                    root.destroy()
                    return
                if glow:
                    glow.show()
        except queue.Empty:
            pass
        root.after(50, poll_queue)

    threading.Thread(target=watcher, daemon=True).start()
    if ENABLE_HOTKEY:
        threading.Thread(target=hotkey_loop, daemon=True).start()
    print("Running. Phone screenshots go to the clipboard."
          + (f" Hotkey: Ctrl+Alt+{HOTKEY_KEY}." if ENABLE_HOTKEY else ""))
    start_tray()
    glow_queue.put(1)  # glow once at launch = "WireSnap is running"
    poll_queue()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log_error()
        message_box(f"WireSnap could not start:\n{e}\n\nDetails are in:\n{LOG_FILE}")
