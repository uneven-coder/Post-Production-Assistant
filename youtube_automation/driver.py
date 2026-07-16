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


class _ContextDead(Exception):
    # Internal signal from _trigger_native_download to download_studio_video: the
    # browser context itself closed (not just a page), so only relaunching the whole
    # browser can recover - never surfaced to callers outside this module.
    pass


def reduce_cuts_for_studio(
    cuts: list[tuple[float, float]], min_duration_s: float = 1.0,
    max_merge_gap_s: float = 0.35, max_edits: int = -1,
) -> list[tuple[float, float]]:
    if not cuts:
        return []
    merged: list[list[float]] = []
    for s, e in sorted(cuts):
        if merged and s - merged[-1][1] <= max_merge_gap_s:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    result = [(s, e) for s, e in merged if e - s >= min_duration_s]

    from silence.detector import cap_intervals
    return cap_intervals(result, max_edits)


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
# hundreds of cuts from a long stream.
#
# el.cuts is a Polymer *computed* property, not the real source of truth - directly
# overwriting it (el.set('cuts', [...])) renders correct-looking rows instantly but
# never creates timeline markers and almost certainly wouldn't survive a real Save;
# confirmed by intercepting el.dispatch/push/splice while performing real cuts through
# the public API. The actual store action is EDITOR_ADD_NEW_CUT, and its payload's
# markersMs is not "this cut's boundaries" - it's the FLAT list of every boundary
# across the whole timeline in order: [0, cut1Start, cut1End, cut2Start, cut2End, ...,
# newCutStart, newCutEnd, videoDurationMs]. Passing the complete list for every cut at
# once (not just the new one) makes a single dispatch create the entire cut list
# correctly, real markers included (verified: markerCount went from 2 to 602 for 300
# cuts, exactly 2 per cut, matching what the slow one-by-one API produces). One
# approveCutById() call on the last cut's id afterwards is what flips whatever flag
# enables Save - confirmed via the same interception that neither dispatch() nor a
# second, unobserved side effect of approveCutById() themselves account for it.
#
# Does NOT hide the timeline/cut-list during this anymore (an earlier version did, as
# a leftover from when applying was a slow per-cut loop). With only one dispatch() +
# one approveCutById() call there's nothing left to hide lag from, and hiding was
# actively counterproductive: confirmed via screenshot that toggling display:none/''
# around the call is what broke the timeline's own reactive redraw (dashed cut-marker
# lines never appeared even after restoring visibility), while leaving the timeline
# visible throughout renders them correctly and immediately, exactly like a real user
# clicking "New Cut" through the UI does.
_APPLY_ALL_CUTS_JS = """
(cuts) => {
  const el = document.querySelector('ytve-trim-options-panel');
  if (!el) return {panelMissing: true};
  if (cuts.length === 0) return {results: []};
  const label = document.getElementById('__pae_progress_label');
  const fill = document.getElementById('__pae_progress_fill');
  const total = cuts.length;

  if (label) label.textContent = 'PAE: applying ' + total + ' cut(s)...';

  let results;
  try {
    const videoDur = el.videoDurationMs;
    const markersMs = [0];
    for (const [startMs, endMs] of cuts) { markersMs.push(startMs, endMs); }
    markersMs.push(videoDur);
    el.dispatch({type: 'EDITOR_ADD_NEW_CUT', payload: {markersMs, cutId: total}});
    el.approveCutById(total);
    results = cuts.map(() => ({ok: true}));
  } catch (e) {
    results = cuts.map(() => ({ok: false, error: String(e)}));
  }

  if (label) label.textContent = 'PAE: ' + total + ' cut(s) applied';
  if (fill) fill.style.width = '100%';
  return {results};
}
"""


def _inject_cut_range_filter(page, total: int) -> None:
    # Reviewing a few hundred cut-rows by scrolling is impractical. Purely visual -
    # only individual `.cut-row` elements in the panel's row list get hidden/shown;
    # `el.cuts` (what Save actually reads) and the timeline itself are untouched.
    js = """
([total]) => {
  const existing = document.getElementById('__pae_range_filter');
  if (existing) existing.remove();

  const wrap = document.createElement('div');
  wrap.id = '__pae_range_filter';
  wrap.style.cssText = 'position:fixed;bottom:16px;left:16px;z-index:2147483647;' +
    'background:#111;color:#ddd;padding:8px 12px;border-radius:6px;' +
    'font-family:Roboto,Arial,sans-serif;font-size:12px;' +
    'display:flex;align-items:center;gap:6px;box-shadow:0 2px 8px rgba(0,0,0,.35);';

  const label = document.createElement('span');
  label.textContent = 'Show cuts:';
  label.style.cssText = 'color:#aaa;';

  const inputCss = 'width:56px;padding:2px 4px;background:#222;color:#fff;' +
    'border:1px solid #444;border-radius:3px;';

  const fromInput = document.createElement('input');
  fromInput.type = 'number';
  fromInput.min = '1';
  fromInput.max = String(total);
  fromInput.value = '1';
  fromInput.style.cssText = inputCss;

  const dash = document.createElement('span');
  dash.textContent = '-';
  dash.style.cssText = 'color:#aaa;';

  const toInput = document.createElement('input');
  toInput.type = 'number';
  toInput.min = '1';
  toInput.max = String(total);
  toInput.value = String(Math.min(total, 30));
  toInput.style.cssText = inputCss;

  const status = document.createElement('span');
  status.style.cssText = 'color:#8f8;margin-left:4px;';

  function applyRange() {
    const rows = document.querySelectorAll('.cut-row');
    let from = Math.max(1, parseInt(fromInput.value, 10) || 1);
    let to = Math.min(rows.length, parseInt(toInput.value, 10) || rows.length);
    if (to < from) to = from;
    rows.forEach((row, i) => {
      const n = i + 1;
      row.style.display = (n >= from && n <= to) ? '' : 'none';
    });
    status.textContent = 'showing ' + from + '-' + to + ' of ' + rows.length;
    window.dispatchEvent(new Event('resize'));
    void document.body.offsetHeight;
  }

  const applyBtn = document.createElement('button');
  applyBtn.textContent = 'Apply';
  applyBtn.style.cssText = 'padding:3px 10px;background:#0e639c;color:#fff;border:none;' +
    'border-radius:4px;cursor:pointer;font-size:11px;';
  applyBtn.onclick = applyRange;

  const allBtn = document.createElement('button');
  allBtn.textContent = 'Show all';
  allBtn.style.cssText = 'padding:3px 10px;background:#3c3c3c;color:#ddd;border:none;' +
    'border-radius:4px;cursor:pointer;font-size:11px;';
  allBtn.onclick = () => {
    fromInput.value = '1';
    toInput.value = String(total);
    applyRange();
  };

  wrap.appendChild(label);
  wrap.appendChild(fromInput);
  wrap.appendChild(dash);
  wrap.appendChild(toInput);
  wrap.appendChild(applyBtn);
  wrap.appendChild(allBtn);
  wrap.appendChild(status);
  document.body.appendChild(wrap);

  applyRange();
}
"""
    try:
        page.evaluate(js, [total])
    except Exception:
        pass


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


_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_START_TIMEOUT_MS = 120000  # per attempt - see retry comment below


def _trigger_native_download(page, context, edit_url: str, report: Callable):
    # Opens the video's "more actions" (⋮) menu and clicks its Download item -
    # studio.youtube.com renders that item as
    # <tp-yt-paper-item test-id="VIDEO_DOWNLOAD"><a href="...download_my_video?...">.
    #
    # Must be a real click wrapped in page.expect_download(), not a GET on the href
    # via a separate requests session with copied cookies.
    #
    # Intermittently, the click doesn't trigger a download at all - instead the tab
    # navigates to Studio's generic channel content list
    # (.../channel/<id>/videos/upload?filter=...&sort=...), and Studio's own JS then
    # appears to close that tab entirely a moment later (observed: a plain retry's
    # page.goto() on the same `page` then fails with "Target page, context or browser
    # has been closed"). Root cause not fully nailed down, but two contributing factors
    # look likely and this guards against both: (1) the menu item can become "visible"
    # before its href is actually populated with the real signed download URL, so
    # clicking too early clicks a stale/empty link; (2) the href's token may itself be
    # time-limited and occasionally goes stale between page load and click. Either way
    # a fresh page load gets a fresh token/menu state, so on failure this reloads (onto
    # a new page if the old one got closed) and retries from scratch.
    page_ref = [page]  # mutable box so the caller can see a page swapped mid-retry

    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        page = page_ref[0]
        if attempt > 1:
            report("download", f"Retrying download (attempt {attempt}/{_DOWNLOAD_ATTEMPTS})...")
            if page.is_closed():
                report("download", "Previous attempt closed the tab - opening a new one.")
                try:
                    page = context.new_page()
                    page.goto(edit_url, wait_until="load")
                except Exception as e:
                    # The whole context (not just the page) died - a persistent-context
                    # profile can auto-close once its last page does. Even a freshly
                    # created page's own .goto() can fail this way if that teardown was
                    # already in flight. No page left to recover with here; only a
                    # fresh browser launch can fix this, which is above this function's
                    # scope - let the caller decide.
                    raise _ContextDead(str(e)) from e
                _inject_progress_bar(page)
                page_ref[0] = page
            else:
                page.goto(edit_url, wait_until="load")

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

        # The item can render before its href is actually populated with the real,
        # signed download URL - wait for something that looks real rather than
        # clicking the instant the element is merely visible.
        href = ""
        for _ in range(20):  # up to ~4s
            href = item.get_attribute("href") or ""
            if "download_my_video" in href:
                break
            page.wait_for_timeout(200)
        if "download_my_video" not in href:
            report("download", "Download link hasn't populated yet - clicking anyway.")

        report("download", "Starting download...")
        try:
            with page.expect_download(timeout=_DOWNLOAD_START_TIMEOUT_MS) as download_info:
                item.click(timeout=10000)
            return download_info.value, page
        except Exception as e:
            if page.is_closed():
                report("download", f"Click closed the tab instead of downloading "
                                    f"(attempt {attempt}/{_DOWNLOAD_ATTEMPTS}).")
            elif "/video/" not in page.url:
                report("download", f"Click navigated to {page.url} instead of downloading "
                                    f"(attempt {attempt}/{_DOWNLOAD_ATTEMPTS}).")
            last_error = e

    raise AutomationError(
        f"Clicking Download didn't produce a browser download after "
        f"{_DOWNLOAD_ATTEMPTS} attempts: {last_error}")


_HTML_SIGNATURES = (b"<!doctype", b"<html")


_BROWSER_SESSION_ATTEMPTS = 2


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
    edit_url = f"{STUDIO_URL}/video/{video_id}/edit"

    # A persistent-context profile can close its whole browser process once its last
    # page closes, not just that page - _trigger_native_download signals this with
    # _ContextDead since no page left in a dead context can recover on its own. When
    # that happens, relaunch the whole browser (fresh context, fresh profile lock) and
    # retry from scratch rather than failing outright.
    for session_attempt in range(1, _BROWSER_SESSION_ATTEMPTS + 1):
        pw, context, profile_root = _open_automation_browser(
            profile_root, profile_name, browser_channel, headless, report)

        page = context.new_page()
        report("navigate", f"Opening {edit_url} ...")
        page.goto(edit_url, wait_until="load")
        _inject_progress_bar(page)
        _update_progress_bar(page, "PAE: locating download link...", pct=0)

        try:
            download, page = _trigger_native_download(page, context, edit_url, report)
            break
        except _ContextDead:
            try:
                _ACTIVE_SESSIONS.remove((pw, context))
            except ValueError:
                pass
            try:
                context.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
            if session_attempt == _BROWSER_SESSION_ATTEMPTS:
                raise AutomationError(
                    "The browser kept closing itself while trying to download this "
                    "video - this may be YouTube Studio blocking repeated automated "
                    "download attempts rather than a one-off glitch. Try again later, "
                    "or download the video manually and place it in the download "
                    "directory.")
            report("download", f"The browser session itself closed - relaunching "
                                f"(session {session_attempt + 1}/{_BROWSER_SESSION_ATTEMPTS})...")

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
    # Applies every cut in one call to _APPLY_ALL_CUTS_JS - see that constant's comment
    # for the single dispatch() + approveCutById() recipe that creates real timeline
    # markers for every cut (not just a fake-looking row list). ~2-4s for 300 cuts, so
    # there's no meaningful "in progress" window left to show incremental progress for.
    if not cuts:
        msg = "No silence cuts to apply - silent_intervals is empty for this session."
        report("cuts", msg)
        _update_progress_bar(page, f"PAE: {msg}", pct=100)
        return msg

    ms_pairs = [(round(max(0.0, s) * 1000), round(min(duration, e) * 1000)) for s, e in cuts]
    total = len(ms_pairs)

    report("cuts", f"Applying {total} cut(s)...")
    _update_progress_bar(page, f"PAE: applying {total} cut(s)...", pct=0)
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
    if cuts:
        _inject_cut_range_filter(page, len(cuts))
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
