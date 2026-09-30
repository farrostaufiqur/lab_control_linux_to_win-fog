"""Windows-only helpers (ctypes + winreg). Imported only on win32."""
import ctypes
import os
import subprocess
import threading
import winreg
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class WindowsPlatform:
    """Implements the `platform` interface expected by labagent.core.Agent."""

    def hostname(self) -> str:
        return os.environ.get("COMPUTERNAME", "unknown")

    def username(self) -> str:
        return os.environ.get("USERNAME", "unknown")

    def foreground(self) -> tuple[str, str]:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return self._process_name(pid.value), buffer.value

    @staticmethod
    def _process_name(pid: int) -> str:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return os.path.basename(buffer.value)
            return ""
        finally:
            kernel32.CloseHandle(handle)

    def idle_seconds(self) -> int:
        info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0
        return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) // 1000

    def uptime_seconds(self) -> int:
        return int(kernel32.GetTickCount64() // 1000)

    def memory_percent(self) -> int:
        status = _MEMORYSTATUSEX(dwLength=ctypes.sizeof(_MEMORYSTATUSEX))
        kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return int(status.dwMemoryLoad)

    def power(self, action: str, force: bool) -> None:
        flag = "/s" if action == "shutdown" else "/r"
        command = ["shutdown", flag, "/t", "3", "/c", "Lab Control"]
        if force:
            command.append("/f")
        subprocess.Popen(command, creationflags=CREATE_NO_WINDOW)


def acquire_single_instance(name: str = "Local\\LabAgentSingleton"):
    """Returns a handle to keep alive, or None if another agent already runs in this session."""
    handle = kernel32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle


# --- window placement -------------------------------------------------------
def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, width, height) of the rectangle covering all monitors."""
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def force_topmost(tk_window_id: int) -> None:
    hwnd = user32.GetParent(tk_window_id) or tk_window_id
    HWND_TOPMOST, flags = wintypes.HWND(-1), 0x0001 | 0x0002 | 0x0040  # NOSIZE | NOMOVE | SHOWWINDOW
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)


# --- keyboard blocking ------------------------------------------------------
HOOKPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = wintypes.LPARAM
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
WH_KEYBOARD_LL = 13


class KeyboardBlocker:
    """Swallows every key press system-wide while `active` (Alt+Tab, Win key, Alt+F4, ...).

    Ctrl+Alt+Del cannot be intercepted by any user-mode program; LockdownPolicies removes
    what that screen can do instead.
    """

    def __init__(self):
        self.active = False
        self._callback = HOOKPROC(self._on_key)  # keep a reference or it gets garbage collected
        threading.Thread(target=self._pump, daemon=True).start()

    def _on_key(self, code, wparam, lparam):
        if code >= 0 and self.active:
            return 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _pump(self):
        user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._callback, kernel32.GetModuleHandleW(None), 0)
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))


# --- registry policies (current user) --------------------------------------
_SYSTEM = r"Software\Microsoft\Windows\CurrentVersion\Policies\System"
_EXPLORER = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"


def _set_policy(subkey: str, name: str, enabled: bool) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1)
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


class LockdownPolicies:
    """Neutralises the Ctrl+Alt+Del screen while the overlay is up."""

    def __init__(self):
        self._state: tuple[bool, bool] | None = None

    def apply(self, locked: bool, block_taskmgr: bool) -> None:
        state = (locked, locked or block_taskmgr)
        if state == self._state:
            return
        previous = self._state or (None, None)
        if state[0] != previous[0]:
            _set_policy(_SYSTEM, "DisableLockWorkstation", locked)
            _set_policy(_SYSTEM, "DisableChangePassword", locked)
            _set_policy(_EXPLORER, "NoLogoff", locked)
        if state[1] != previous[1]:
            _set_policy(_SYSTEM, "DisableTaskMgr", state[1])
        self._state = state


# --- pemulihan darurat: cek drive & copot agent (dipakai killswitch.py) --------
DRIVE_REMOVABLE, DRIVE_FIXED = 2, 3
SEM_FAILCRITICALERRORS = 0x0001
kernel32.GetLogicalDrives.restype = wintypes.DWORD
kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
kernel32.SetErrorMode.argtypes = [wintypes.UINT]


def list_drives() -> dict[str, int]:
    """{akar drive: nomor seri volume} untuk disk tetap dan removable yang medianya sudah terbaca."""
    kernel32.SetErrorMode(SEM_FAILCRITICALERRORS)  # tanpa dialog "Insert a disk"
    mask, drives = kernel32.GetLogicalDrives(), {}
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        if kernel32.GetDriveTypeW(root) not in (DRIVE_REMOVABLE, DRIVE_FIXED):
            continue
        serial = wintypes.DWORD()
        if kernel32.GetVolumeInformationW(root, None, 0, ctypes.byref(serial), None, None, None, 0):
            drives[root] = serial.value
    return drives


def list_names(root: str) -> list[str] | None:
    """Nama file/folder tingkat pertama saja, tanpa membuka isinya. None bila drive belum terbaca."""
    try:
        return os.listdir(root)
    except OSError:
        return None


def remove_agent(install_dir: str, state_dir: str) -> None:
    """Mencopot agent sepenuhnya: tugas terjadwal, proses, kebijakan registry, lalu folder."""
    from .killswitch import cleanup_script

    def run(*command):
        subprocess.run(command, creationflags=CREATE_NO_WINDOW, capture_output=True)

    # tugas dihapus dulu supaya watchdog tidak menjalankan agent lagi
    for task in ("LabAgent", "LabAgentKillSwitch"):
        run("schtasks", "/Delete", "/TN", task, "/F")
    # kecualikan diri sendiri dan proses induknya (bootloader PyInstaller onefile)
    run("taskkill", "/F", "/IM", "labagent.exe", "/FI", f"PID ne {os.getpid()}", "/FI", f"PID ne {os.getppid()}")

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SYSTEM, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "HideFastUserSwitching")
    except OSError:
        pass
    index = 0
    while True:  # profil user yang sedang termuat
        try:
            sid = winreg.EnumKey(winreg.HKEY_USERS, index)
        except OSError:
            break
        index += 1
        if not sid.startswith("S-1-5-21-") or sid.endswith("_Classes"):
            continue
        for subkey, names in ((_SYSTEM, ("DisableTaskMgr", "DisableLockWorkstation", "DisableChangePassword")),
                              (_EXPLORER, ("NoLogoff",))):
            try:
                with winreg.OpenKey(winreg.HKEY_USERS, f"{sid}\\{subkey}", 0, winreg.KEY_SET_VALUE) as key:
                    for name in names:
                        try:
                            winreg.DeleteValue(key, name)
                        except OSError:
                            pass
            except OSError:
                pass

    script = os.path.join(os.environ.get("TEMP", state_dir), "labagent_cleanup.cmd")
    with open(script, "w", encoding="ascii", errors="replace") as f:
        f.write(cleanup_script(install_dir, state_dir))
    subprocess.Popen(["cmd", "/c", script], creationflags=CREATE_NO_WINDOW | 0x00000008)  # DETACHED_PROCESS
