import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

ProgressCallback = Optional[Callable[[str, str], None]]  # (step, message)

STUDIO_URL = "https://studio.youtube.com"

DEFAULT_AUTOMATION_PROFILE_ROOT = os.path.join(os.path.expanduser("~"), ".pae_chrome_profile")

# Keeps the browser/driver alive after run_automation() returns; otherwise garbage
# collection would close the visible window along with it.
_ACTIVE_SESSIONS: list = []


class AutomationError(RuntimeError):
    pass


def reduce_cuts_for_studio(
    cuts: list[tuple[float, float]], min_duration_s: float = 1.0,
    max_merge_gap_s: float = 0.35,
) -> list[tuple[float, float]]:
    if not cuts:
        return []
    merged: list[list[float]] = []
    for s, e in sorted(cuts):
        if merged and s - merged[-1][1] <= max_merge_gap_s:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= min_duration_s]


def _find_chrome_exe_path() -> Optional[str]:
    if sys.platform != "win32":
        return None
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    for base in (pf, pf86, local):
        if not base:
            continue
        candidate = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
        if os.path.exists(candidate):
            return candidate
    return None


def _looks_signed_in(profile_root: str, profile_name: str) -> bool:
    profile_dir = os.path.join(profile_root, profile_name)
    for rel in ("Network/Cookies", "Cookies"):  # Chrome moved this path at some point
        try:
            if os.path.getsize(os.path.join(profile_dir, rel)) > 4096:
                return True
        except OSError:
            continue
    return False


def _launch_plain_chrome_for_login(profile_root: str, profile_name: str) -> bool:
    exe = _find_chrome_exe_path()
    if not exe:
        return False
    subprocess.Popen([exe, f"--user-data-dir={profile_root}", f"--profile-directory={profile_name}",
                      "https://accounts.google.com/signin"])
    return True


def _profile_lock_path(profile_root: str) -> str:
    return os.path.join(profile_root, "SingletonLock")


def _run_elevated_and_wait(exe: str, params: str, timeout_s: float = 240.0) -> int:
    # Runs `exe params` elevated via UAC and blocks until it exits; returns the exit
    # code, -1 if declined/failed to launch, or -2 on timeout.
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
            ("hKeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_SHOW = 1
    WAIT_TIMEOUT = 0x00000102

    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = SW_SHOW

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)) or not sei.hProcess:
        return -1

    result = ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, int(timeout_s * 1000))
    if result == WAIT_TIMEOUT:
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return -2

    exit_code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(exit_code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return exit_code.value


def _is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _install_chrome_for_playwright(report: Callable) -> None:
    report("launch", "Chrome not found - installing it via Playwright (one-time, ~100MB)...")
    result = subprocess.run([sys.executable, "-m", "playwright", "install", "chrome"],
                            capture_output=True, text=True)
    if result.returncode == 0:
        report("launch", "Chrome installed.")
        return

    output = result.stderr or result.stdout
    if not ("privileges" in output.lower() or "administrator" in output.lower()) \
            or sys.platform != "win32":
        raise AutomationError(f"Failed to install Chrome via Playwright: {output[-800:]}")

    report("launch", "Needs admin rights - requesting elevation")
    install_cmd = f'"{sys.executable}" -m playwright install chrome'
    params = f'/c {install_cmd} & echo. & echo Done - you can close this window. & pause'
    code = _run_elevated_and_wait("cmd.exe", params)
    if code == -1:
        raise AutomationError(
            "Admin permission was declined (or the prompt couldn't be shown) - install "
            "Google Chrome normally instead (a regular per-user install from "
            "https://www.google.com/chrome/ needs no admin rights), then try again.")
    if code == -2:
        raise AutomationError("The elevated Chrome install is taking a while - let it "
                              "finish in its own window, then try again.")
    if code != 0:
        raise AutomationError(f"Elevated Chrome install exited with code {code} - check "
                              "the terminal window for details.")
    report("launch", "Chrome installed.")


def _launch_context(profile_root: str, profile_name: str, channel: str, headless: bool,
                    report: Callable):
    from playwright.sync_api import sync_playwright

    if os.path.exists(_profile_lock_path(profile_root)):
        raise AutomationError(
            "An automation browser window is already open for this profile - close it "
            "first, then try again.")

    pw = sync_playwright().start()
    args = [f"--profile-directory={profile_name}"]

    def _try_launch():
        return pw.chromium.launch_persistent_context(
            profile_root, channel=channel, headless=headless, args=args,
            no_viewport=True, timeout=30000,
            # Both flags get Google's sign-in page to reject the browser as insecure.
            ignore_default_args=["--enable-automation", "--no-sandbox"])

    try:
        context = _try_launch()
        return pw, context
    except Exception as e:
        msg = str(e)
        if "is not found" in msg.lower() or "playwright install" in msg.lower():
            _install_chrome_for_playwright(report)
            try:
                return pw, _try_launch()
            except Exception as e2:
                pw.stop()
                raise AutomationError(f"Still couldn't launch Chrome after installing it: {e2}")
        pw.stop()
        if "singleton" in msg.lower() or ("profile" in msg.lower() and "use" in msg.lower()):
            raise AutomationError(
                f"Chrome already has '{profile_name}' open - close every Chrome window and "
                "try again, so automation can take control of that profile.")
        raise AutomationError(f"Couldn't launch Chrome for automation: {e}")


def _find_browser_root_pid(profile_root: str) -> Optional[int]:
    # PID of this automation's own chrome.exe, matched by --user-data-dir, so the
    # file-dialog watcher never acts on a dialog from some other Chrome window.
    if sys.platform != "win32":
        return None
    try:
        needle = profile_root.replace("'", "''")
        cmd = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
               f"Where-Object {{ $_.CommandLine -like '*--user-data-dir={needle}*' }} | "
               "Select-Object -First 1 -ExpandProperty ProcessId")
        result = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                                 capture_output=True, text=True, timeout=10)
        pid = result.stdout.strip()
        return int(pid) if pid.isdigit() else None
    except Exception:
        return None


def _window_process_id(hwnd: int) -> int:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _find_open_file_dialog(root_pid: int) -> Optional[int]:
    matches: list = []

    def _enum(hwnd, _lparam):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "#32770":  # native common-dialog window class
            return True
        if _window_process_id(hwnd) == root_pid:
            matches.append(hwnd)
            return True
        owner = ctypes.windll.user32.GetWindow(hwnd, 4)  # GW_OWNER
        if owner and _window_process_id(owner) == root_pid:
            matches.append(hwnd)
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_enum)
    ctypes.windll.user32.EnumWindows(enum_proc, 0)
    return matches[0] if matches else None


def _force_foreground(hwnd: int) -> bool:
    # Plain SetForegroundWindow from a background process is routinely ignored by
    # Windows' foreground-lock rules; attaching thread input is the standard workaround.
    user32 = ctypes.windll.user32
    cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)

    attached = []
    for tid in (fg_tid, target_tid):
        if tid and tid != cur_tid and user32.AttachThreadInput(cur_tid, tid, True):
            attached.append(tid)
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE, in case minimized
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for tid in attached:
            user32.AttachThreadInput(cur_tid, tid, False)
    return user32.GetForegroundWindow() == hwnd


def _watch_for_file_dialog(video_path: str, root_pid: Optional[int], report: Callable,
                            stop_event: threading.Event) -> None:
    # Runs on its own thread outside Playwright for the browser session's lifetime,
    # filling in Chrome's native file-picker the moment it opens.
    if root_pid is None:
        report("upload", "Couldn't find this automation's Chrome process - automatic "
                          "file-dialog fill-in is disabled; type the path in yourself.")
        return
    try:
        import pyautogui
    except ImportError:
        report("upload", "pyautogui isn't installed - automatic file-dialog fill-in is "
                          "disabled (pip install pyautogui to enable it).")
        return

    last_hwnd = None
    while not stop_event.is_set():
        hwnd = _find_open_file_dialog(root_pid)
        if hwnd and hwnd != last_hwnd:
            last_hwnd = hwnd
            try:
                for _ in range(5):
                    if _force_foreground(hwnd):
                        break
                    time.sleep(0.15)
                time.sleep(0.2)
                pyautogui.hotkey("ctrl", "a")
                pyautogui.write(video_path, interval=0.01)
                pyautogui.press("enter")
                report("upload", f"Detected the file dialog and inserted: "
                                  f"{os.path.basename(video_path)}")
            except Exception as e:
                report("upload", f"Found the file dialog but couldn't fill it in: {e}")
        elif not hwnd:
            last_hwnd = None
        stop_event.wait(0.3)


_APPLY_CUTS_JS = """
(cuts) => {
  const el = document.querySelector('ytve-trim-options-panel');
  if (!el) return {panelMissing: true};
  const results = [];
  for (const [startMs, endMs] of cuts) {
    try {
      el.addNewCutAtTime(startMs);
      const idx = el.cuts.length - 1;
      const cutId = el.cuts[idx].id;
      el.set('cuts.' + idx + '.endMs', endMs);
      el.approveCutById(cutId);
      results.push({ok: true});
    } catch (e) {
      results.push({ok: false, error: String(e)});
    }
  }
  return {results};
}
"""


# Used only by the auto-apply (finish_editing_existing_video) path, which can see
# hundreds of cuts from a long stream. Studio's own Trim & cut panel re-renders the
# timeline canvas and the cut-row list after every single insertion, and that (not our
# own per-cut JS calls) is what dominates the time and makes the tab look laggy for a
# big batch. So the timeline/cut-list are hidden (display:none) and a "paused" message
# shown in their place for the one plain, synchronous loop that inserts every cut,
# restoring both once done - no async/await, no chunking, all cuts in a single call.
_APPLY_ALL_CUTS_JS = """
(cuts) => {
  const el = document.querySelector('ytve-trim-options-panel');
  if (!el) return {panelMissing: true};
  const label = document.getElementById('__pae_progress_label');
  const fill = document.getElementById('__pae_progress_fill');
  const total = cuts.length;

  const hidden = [document.getElementById('timeline-section'),
                  document.getElementById('panel-container')].filter(Boolean);
  const prevDisplay = hidden.map((t) => t.style.display);
  for (const t of hidden) t.style.display = 'none';
  if (label) label.textContent = 'PAE: UI paused - applying ' + total + ' cut(s)...';

  const results = [];
  try {
    for (let i = 0; i < cuts.length; i++) {
      const [startMs, endMs] = cuts[i];
      try {
        el.addNewCutAtTime(startMs);
        const idx = el.cuts.length - 1;
        const cutId = el.cuts[idx].id;
        el.set('cuts.' + idx + '.endMs', endMs);
        el.approveCutById(cutId);
        results.push({ok: true});
      } catch (e) {
        results.push({ok: false, error: String(e)});
      }

      const done = i + 1;
      const pct = Math.round(done * 100 / total);
      if (label) label.textContent = 'PAE: UI paused - applying cuts... ' + done + '/' + total + ' (' + pct + '%)';
      if (fill) fill.style.width = pct + '%';
    }
  } finally {
    hidden.forEach((t, i) => { t.style.display = prevDisplay[i]; });
  }
  return {results};
}
"""


def _safe_reinject(page, init_js: str) -> None:
    try:
        page.evaluate(init_js)
    except Exception:
        pass


_UPLOAD_HINT = ("PAE: video auto-attaches once you open Chrome's file picker - use this "
                "button once you're on the Cuts tab")


def _inject_helper_buttons(page, report: Callable, cuts: list, duration: float,
                           hint_text: str = _UPLOAD_HINT):
    # expose_function callbacks run on Playwright's own dispatcher thread; calling other
    # sync-API methods from inside one deadlocks it. So the exposed function only
    # enqueues the request - _pump_button_actions does the actual work.
    action_queue: "queue.Queue[str]" = queue.Queue()

    def _report_cuts(msg: str) -> None:
        # Only safe from _pump_button_actions's thread, never the expose_function
        # callback. Mirrors onto the on-page status line as well as the app log.
        report("cuts", msg)
        try:
            page.evaluate(
                "(msg) => { const el = document.getElementById('__pae_status'); "
                "if (el) el.textContent = msg; }", msg)
        except Exception:
            pass

    def _apply_cuts() -> str:
        if not cuts:
            _report_cuts("No silence cuts to apply - silent_intervals is empty for this "
                         "session (run silence detection first).")
            return "No silence cuts to apply"

        ms_pairs = [(round(max(0.0, s) * 1000), round(min(duration, e) * 1000)) for s, e in cuts]
        _report_cuts(f"Applying {len(ms_pairs)} cut(s)...")
        try:
            outcome = page.evaluate(_APPLY_CUTS_JS, ms_pairs)
        except Exception as ex:
            _report_cuts(f"Failed to apply cuts - {ex}")
            return f"Error applying cuts: {ex}"

        if outcome.get("panelMissing"):
            msg = ("Couldn't find the Trim & cut panel on this page - make sure you're "
                   "on Editor > Trim & cut before clicking Edit Timeline.")
            _report_cuts(msg)
            return msg

        results = outcome["results"]
        failed = [(i, r) for i, r in enumerate(results, 1) if not r.get("ok")]
        ok_count = len(results) - len(failed)

        if failed:
            first_i, first = failed[0]
            msg = (f"{ok_count}/{len(cuts)} cuts applied - {len(failed)} failed "
                   f"(first at cut {first_i}: {first.get('error')})")
            _report_cuts(msg)
            return msg

        _report_cuts("All cuts marked up - review and click Save yourself.")
        return f"{ok_count} cuts applied - review and Save"

    def trigger_apply_cuts() -> str:
        report("cuts", "Edit Timeline clicked - queued for processing...")
        action_queue.put("apply_cuts")
        return "queued"

    try:
        page.expose_function("paeApplyCuts", trigger_apply_cuts)
    except Exception as e:
        report("cuts", f"Warning: couldn't bind the Edit Timeline button's Python "
                        f"callback ({e}).")

    init_js = """
(() => {
  if (location.hostname !== 'studio.youtube.com') return;
  function inject() {
    if (!document.body) return;
    if (!document.getElementById('__pae_button_bar')) {
      const wrap = document.createElement('div');
      wrap.id = '__pae_button_bar';
      wrap.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:2147483647;' +
        'display:flex;flex-direction:column;align-items:flex-end;gap:6px;' +
        'font-family:Roboto,Arial,sans-serif;';
      const hint = document.createElement('div');
      hint.textContent = "__PAE_HINT__";
      hint.style.cssText = 'background:#222;color:#ddd;padding:4px 10px;border-radius:4px;' +
        'font-size:11px;box-shadow:0 2px 8px rgba(0,0,0,.35);';
      const bar = document.createElement('div');
      bar.style.cssText = 'display:flex;gap:8px;';
      const b = document.createElement('button');
      b.textContent = 'Edit Timeline (__PAE_CUT_COUNT__)';
      b.style.cssText = 'padding:10px 16px;background:#0e639c;color:#fff;border:none;' +
        'border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;' +
        'box-shadow:0 2px 8px rgba(0,0,0,.35);';
      b.onclick = async () => {
        const original = b.textContent;
        b.disabled = true;
        b.textContent = 'Working...';
        // Exposed functions only enqueue and return instantly - the real result
        // arrives later via window.__paeSetResult().
        window.__paeSetResult = (msg) => {
          b.textContent = msg;
          setTimeout(() => { b.disabled = false; b.textContent = original; }, 4000);
        };
        try {
          await window.paeApplyCuts();
        } catch (e) {
          console.error('[PAE] trigger call failed:', e);
          b.textContent = 'Error: ' + (e && e.message ? e.message : e);
          setTimeout(() => { b.disabled = false; b.textContent = original; }, 4000);
        }
      };
      bar.appendChild(b);
      const status = document.createElement('div');
      status.id = '__pae_status';
      status.textContent = '__PAE_CUT_COUNT__ cut(s) loaded - click Edit Timeline to apply';
      status.style.cssText = 'background:#111;color:#8f8;padding:4px 10px;border-radius:4px;' +
        'font-size:11px;max-width:380px;text-align:right;box-shadow:0 2px 8px rgba(0,0,0,.35);';
      wrap.appendChild(hint);
      wrap.appendChild(bar);
      wrap.appendChild(status);
      document.body.appendChild(wrap);
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
  // Studio's SPA routing can wipe/rebuild document.body without a real navigation, and
  // sometimes wholesale-replaces it - a MutationObserver bound to the old body node goes
  // silently stale in that case. A plain interval re-check always looks up document.body
  // fresh, so it can't get stuck watching a detached node.
  if (!window.__paeWatcher) {
    window.__paeWatcher = setInterval(inject, 1000);
  }
})();
"""
    init_js = init_js.replace("__PAE_CUT_COUNT__", str(len(cuts))).replace("__PAE_HINT__", hint_text)
    page.add_init_script(init_js)  # survives full reloads/navigations
    _safe_reinject(page, init_js)  # and applies immediately to what's already loaded
    page.on("framenavigated", lambda frame: (
        _safe_reinject(page, init_js) if frame == page.main_frame else None))

    return action_queue, _apply_cuts


def _pump_all_pages(context, report: Callable, cuts: list, duration: float,
                     new_pages: "queue.Queue") -> None:
    # Drives every tab's Edit Timeline button from one thread (Playwright's sync API only
    # works from the thread that owns it), covering tabs restored, already open, or opened
    # later. Also pumps Playwright's sync machinery via page.evaluate("1") so pending
    # expose_function callbacks actually resolve.
    sessions: dict = {}

    def _setup(p) -> None:
        # Idempotent: the initial context.pages enumeration and a context.on("page", ...)
        # event can both see the same page in a startup race - re-injecting would leave
        # window.paeApplyCuts bound to a different (orphaned) queue than sessions[p] ends
        # up tracking, so clicks would silently go nowhere.
        if p in sessions or p.is_closed():
            return
        try:
            action_queue, apply_cuts = _inject_helper_buttons(p, report, cuts, duration)
            sessions[p] = (action_queue, apply_cuts)
        except Exception as e:
            report("cuts", f"Couldn't set up a browser tab for automation: {e}")

    for p in list(context.pages):
        _setup(p)

    while True:
        while True:
            try:
                _setup(new_pages.get_nowait())
            except queue.Empty:
                break

        if not context.pages:
            return

        acted = False
        for page, (action_queue, apply_cuts) in list(sessions.items()):
            if page.is_closed():
                sessions.pop(page, None)
                continue
            try:
                action_queue.get_nowait()
            except queue.Empty:
                continue
            acted = True
            result = apply_cuts()
            try:
                page.evaluate("(msg) => window.__paeSetResult && window.__paeSetResult(msg)", result)
            except Exception:
                sessions.pop(page, None)
        if acted:
            continue

        pumped = False
        for page in list(sessions.keys()):
            if page.is_closed():
                sessions.pop(page, None)
                continue
            try:
                page.evaluate("1")
                pumped = True
                break
            except Exception:
                sessions.pop(page, None)
        if not pumped and not sessions:
            return
        time.sleep(0.2)


def _open_automation_browser(profile_root: Optional[str], profile_name: str,
                             browser_channel: str, headless: bool, report: Callable):
    # Shared by run_automation and download_studio_video - both need the identical
    # dedicated automation profile (login is a one-time manual step since Google blocks
    # CDP-attached sign-ins), just pointed at different pages afterwards.
    if _is_elevated():
        report("launch", "Warning: this is running as Administrator - Chrome disables its "
                          "sandbox when launched from an elevated process (you'll see its "
                          "'unsupported command-line flag: --no-sandbox' banner) and can "
                          "behave oddly. Run PAE itself from a normal, non-elevated terminal "
                          "- admin rights are only needed for the one-time Chrome install.")

    profile_root = profile_root or DEFAULT_AUTOMATION_PROFILE_ROOT
    os.makedirs(profile_root, exist_ok=True)

    if not _looks_signed_in(profile_root, profile_name):
        if _launch_plain_chrome_for_login(profile_root, profile_name):
            raise AutomationError(
                "First-time setup: a plain, non-automated Chrome window just opened - sign "
                "into your Google/YouTube account there normally (has to be a genuinely "
                "manual login; Google blocks automation-driven sign-ins outright), then "
                "close that window and run automation again.")
        raise AutomationError(
            "This automation profile isn't signed in yet, and no Chrome install could be "
            f"found to open it for a manual first-time login. Install Chrome, or sign in "
            f"yourself by running: chrome.exe --user-data-dir=\"{profile_root}\" "
            f"--profile-directory=\"{profile_name}\" - then try again.")

    report("launch", f"Opening Chrome (profile: {profile_root}\\{profile_name})...")
    pw, context = _launch_context(profile_root, profile_name, browser_channel, headless, report)
    report("launch", "Browser ready.")
    _ACTIVE_SESSIONS.append((pw, context))
    return pw, context, profile_root


def run_automation(
    video_path: str, cuts: list, duration: float, *,
    profile_root: str = None, profile_name: str = "Default",
    browser_channel: str = "chrome", headless: bool = False,
    progress_callback: ProgressCallback = None,
) -> None:
    # Opens YouTube Studio; a background thread fills in Chrome's file-picker and a
    # floating "Edit Timeline" button applies `cuts` on every open Cuts tab. Runs
    # synchronously until every browser tab is closed. Uses a dedicated automation
    # profile - login is a one-time manual step since Google blocks CDP-attached sign-ins.
    def report(step: str, message: str):
        if progress_callback:
            progress_callback(step, message)

    if not os.path.exists(video_path):
        raise AutomationError(f"Video file not found: {video_path}")

    try:
        pw, context, profile_root = _open_automation_browser(
            profile_root, profile_name, browser_channel, headless, report)

        # Registered before reading context.pages, so a tab created in that exact gap
        # (e.g. Chrome still restoring a previous session) can't be missed by both.
        new_pages: "queue.Queue" = queue.Queue()
        context.on("page", lambda p: new_pages.put(p))

        existing = context.pages
        page = existing[0] if existing else context.new_page()
        already_on_studio = any(STUDIO_URL in (p.url or "") for p in context.pages)

        root_pid = _find_browser_root_pid(profile_root)
        dialog_stop = threading.Event()
        dialog_thread = threading.Thread(
            target=_watch_for_file_dialog, args=(video_path, root_pid, report, dialog_stop),
            daemon=True)
        dialog_thread.start()

        if not already_on_studio:
            report("navigate", "Opening YouTube Studio...")
            page.goto(STUDIO_URL, wait_until="load")
        report("done", "Ready - the browser behaves normally; a floating button appears "
                       "bottom-right on every Studio tab, including ones restored from a "
                       "previous session or opened later. Use Create > Upload videos "
                       "yourself to reach the file-picker screen, then click its own "
                       "'SELECT FILES' button (or drop a file on its drag-and-drop zone) "
                       "- the video attaches automatically the moment that dialog opens. "
                       "Once you're on that video's Editor > Cuts tab, click 'Edit "
                       "Timeline' to mark up the detected silence.")
        try:
            _pump_all_pages(context, report, cuts, duration, new_pages)
        finally:
            dialog_stop.set()
    except AutomationError as e:
        report("error", str(e))
        raise
    except Exception as e:
        report("error", f"Unexpected error: {e}")
        raise


_PROGRESS_BAR_BUILD_JS = """
  const bar = document.createElement('div');
  bar.id = '__pae_progress_bar';
  bar.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:2147483647;' +
    'background:#111;color:#8f8;padding:8px 14px;border-radius:6px;' +
    'font-family:Roboto,Arial,sans-serif;font-size:12px;max-width:340px;' +
    'text-align:right;box-shadow:0 2px 8px rgba(0,0,0,.35);';
  const barLabel = document.createElement('div');
  barLabel.id = '__pae_progress_label';
  barLabel.textContent = 'PAE: starting...';
  const barTrack = document.createElement('div');
  barTrack.style.cssText = 'width:100%;height:4px;background:#333;border-radius:2px;' +
    'margin-top:6px;overflow:hidden;';
  const barFill = document.createElement('div');
  barFill.id = '__pae_progress_fill';
  barFill.style.cssText = 'height:100%;width:0%;background:#4caf50;transition:width .25s ease;';
  barTrack.appendChild(barFill);
  bar.appendChild(barLabel);
  bar.appendChild(barTrack);
  document.body.appendChild(bar);
"""


def _inject_progress_bar(page) -> None:
    js = "(() => { if (document.getElementById('__pae_progress_bar')) return;" + _PROGRESS_BAR_BUILD_JS + "})();"
    try:
        page.evaluate(js)
    except Exception:
        pass


def _update_progress_bar(page, message: str, pct: Optional[float] = None) -> None:
    js = (
        "([msg, pct]) => {"
        "  let el = document.getElementById('__pae_progress_bar');"
        "  if (!el && document.body) {" + _PROGRESS_BAR_BUILD_JS + "el = document.getElementById('__pae_progress_bar'); }"
        "  if (!el) return;"
        "  const label = el.querySelector('#__pae_progress_label');"
        "  if (label) label.textContent = msg;"
        "  if (pct !== null && pct !== undefined) {"
        "    const fill = el.querySelector('#__pae_progress_fill');"
        "    if (fill) fill.style.width = Math.max(0, Math.min(100, pct)) + '%';"
        "  }"
        "}"
    )
    try:
        page.evaluate(js, [message, pct])
    except Exception:
        pass


def _remove_progress_bar(page) -> None:
    try:
        page.evaluate(
            "() => { const el = document.getElementById('__pae_progress_bar'); "
            "if (el) el.remove(); }")
    except Exception:
        pass


def _notify_done() -> None:
    if sys.platform != "win32":
        return
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


_MENU_BUTTON_SELECTOR = (
    # Verified against the live Details (/edit) page: the video-level actions menu is
    # <ytcp-icon-button id="overflow-menu-button" aria-label="Options"> inside
    # <ytcp-video-overflow-menu>. Studio also renders several other aria-label="Options"
    # buttons on the same page (suggestion panels, thumbnail rows) that are hidden via
    # CSS rather than absent - ":visible" and the specific id keep .first from landing on
    # one of those instead.
    'ytcp-icon-button#overflow-menu-button:visible, '
    'ytcp-video-overflow-menu [aria-label="Options"]:visible, '
    'ytcp-icon-button#menu-button:visible, ytcp-button#menu-button:visible, '
    'button[aria-label*="More actions" i]:visible, button[aria-label="Options"]:visible'
)


def _trigger_native_download(page, report: Callable):
    # Opens the video's "more actions" (⋮) menu and clicks its Download item -
    # studio.youtube.com renders that item as
    # <tp-yt-paper-item test-id="VIDEO_DOWNLOAD"><a href="...download_my_video?...">.
    #
    # Must be a real click wrapped in page.expect_download(), not a GET on the href
    # via a separate requests session with copied cookies
    report("download", "Opening the video's options menu...")
    menu_button = page.locator(_MENU_BUTTON_SELECTOR).first
    try:
        menu_button.wait_for(state="visible", timeout=20000)
        menu_button.click(timeout=15000)
    except Exception as e:
        raise AutomationError(
            "Couldn't find the video's options (...) menu button - YouTube Studio's layout "
            "may have changed. Download the video manually and place it in the download "
            f"directory, or check youtube_automation/driver.py's _MENU_BUTTON_SELECTOR. ({e})")

    item = page.locator('[test-id="VIDEO_DOWNLOAD"] a').first
    try:
        item.wait_for(state="visible", timeout=15000)
    except Exception:
        raise AutomationError(
            "Couldn't find the Download option in the video's options menu - YouTube "
            "Studio's layout may have changed. Download the video manually and place it "
            "in the download directory, or check youtube_automation/driver.py's "
            "_trigger_native_download() selectors.")

    report("download", "Starting download...")
    try:
        with page.expect_download(timeout=600000) as download_info:
            item.click(timeout=10000)
    except Exception as e:
        raise AutomationError(f"Clicking Download didn't produce a browser download: {e}")
    return download_info.value


_HTML_SIGNATURES = (b"<!doctype", b"<html")


def download_studio_video(
    video_id: str, dest_dir: str, *,
    profile_root: str = None, profile_name: str = "Default",
    browser_channel: str = "chrome", headless: bool = False,
    progress_callback: ProgressCallback = None,
):

    def report(step, message):
        if progress_callback:
            progress_callback(step, message)

    os.makedirs(dest_dir, exist_ok=True)

    pw, context, profile_root = _open_automation_browser(
        profile_root, profile_name, browser_channel, headless, report)

    page = context.new_page()
    edit_url = f"{STUDIO_URL}/video/{video_id}/edit"
    report("navigate", f"Opening {edit_url} ...")
    page.goto(edit_url, wait_until="load")
    _inject_progress_bar(page)
    _update_progress_bar(page, "PAE: locating download link...", pct=0)

    download = _trigger_native_download(page, report)

    dest_path = os.path.join(dest_dir, f"{video_id}.mp4")
    report("download", "Downloading (can take a while for large videos)...")
    _update_progress_bar(page, "PAE: downloading... please wait")
    download.save_as(dest_path)

    if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
        raise AutomationError("Download finished but produced an empty file - try again.")
    with open(dest_path, "rb") as f:
        head = f.read(32).lstrip().lower()
    if any(head.startswith(sig) for sig in _HTML_SIGNATURES):
        raise AutomationError(
            "The download produced an HTML page instead of a video file - YouTube "
            "Studio's download flow may have changed. Download the video manually "
            f"and place it at {dest_path}, or check youtube_automation/driver.py's "
            "_trigger_native_download().")

    report("download", f"Downloaded: {os.path.basename(dest_path)}")
    _update_progress_bar(page, "PAE: download complete - removing silence...", pct=0)
    return dest_path, pw, context, page


def _apply_cuts_with_progress(page, cuts: list, duration: float, report: Callable) -> str:
    # Applies every cut in one plain, synchronous call to _APPLY_ALL_CUTS_JS, which
    # hides the timeline/cut-list for the duration (that's what actually causes the lag,
    # not our own per-cut calls - see the JS constant's comment) and shows a "paused"
    # message on the progress bar in their place, restoring both once done. No
    # async/await and no chunking on either side - console-side progress is necessarily
    # coarser than a chunked approach (start and final result only), but the on-page bar
    # still updates live, per cut, from inside that one call.
    if not cuts:
        msg = "No silence cuts to apply - silent_intervals is empty for this session."
        report("cuts", msg)
        _update_progress_bar(page, f"PAE: {msg}", pct=100)
        return msg

    ms_pairs = [(round(max(0.0, s) * 1000), round(min(duration, e) * 1000)) for s, e in cuts]
    total = len(ms_pairs)

    report("cuts", f"Applying {total} cut(s) - UI paused for the duration...")
    _update_progress_bar(page, f"PAE: UI paused - applying {total} cut(s)...", pct=0)
    try:
        outcome = page.evaluate(_APPLY_ALL_CUTS_JS, ms_pairs)
    except Exception as ex:
        msg = f"Failed to apply cuts - {ex}"
        report("cuts", msg)
        _update_progress_bar(page, f"PAE: {msg}")
        return msg

    if outcome.get("panelMissing"):
        msg = ("Couldn't find the Trim & cut panel on this page - Studio's layout "
               "may have changed.")
        report("cuts", msg)
        _update_progress_bar(page, f"PAE: {msg}")
        return msg

    results = outcome["results"]
    failed = [(i, r) for i, r in enumerate(results, 1) if not r.get("ok")]
    applied = len(results) - len(failed)

    if failed:
        first_i, first = failed[0]
        msg = (f"{applied}/{total} cuts applied - {len(failed)} failed "
               f"(first at cut {first_i}: {first.get('error')})")
    else:
        msg = f"{applied} cuts applied - review and Save"

    _update_progress_bar(page, f"PAE: {msg}", pct=100)
    report("cuts", msg)
    return msg


def finish_editing_existing_video(
    page, video_id: str, cuts: list, duration: float, *,
    progress_callback: ProgressCallback = None,
) -> None:
    def report(step, message):
        if progress_callback:
            progress_callback(step, message)

    editor_url = f"{STUDIO_URL}/video/{video_id}/editor"
    report("navigate", f"Opening the editor: {editor_url} ...")
    page.goto(editor_url, wait_until="load")
    _inject_progress_bar(page)
    _update_progress_bar(page, "PAE: entering Trim & cut...", pct=0)

    try:
        page.locator('a:has-text("Trim & cut")').first.click(timeout=15000)
        page.locator("#new-cut-button").wait_for(state="visible", timeout=15000)
    except Exception as e:
        raise AutomationError(
            "Couldn't enter the Trim & cut editor - YouTube Studio's layout may have "
            f"changed. Open Editor > Trim & cut yourself and apply the cuts manually. ({e})")

    result = _apply_cuts_with_progress(page, cuts, duration, report)
    _notify_done()
    report("done", result)


def close_all_sessions() -> None:
    while _ACTIVE_SESSIONS:
        pw, context = _ACTIVE_SESSIONS.pop()
        try:
            context.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
