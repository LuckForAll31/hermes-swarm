"""Per-team persistent, shared browser pool.

Each team gets ONE long-lived Chrome (DevTools/CDP) bound to a stable
``--user-data-dir`` under ``data/teams/<team>/.browser-profile``. Every agent in
that team is pointed at the same ``browser.cdp_url`` (written into its
config.yaml), so they share one browser — same cookies, logins, and storage.

Durability: because the profile directory is a fixed path on disk, the browser's
state survives a server restart. On restart we relaunch Chrome against the same
profile dir; cookies/logins are still there. (Chrome itself only allows one
process per user-data-dir, which is exactly the one-browser-per-team invariant.)

Isolation: one profile per team => teams never see each other's cookies/sessions.

Display & human takeover: the browser is ALWAYS rendered on a dedicated, hidden
Xvfb display (one per team) — never on the host's real desktop. The agent drives
it over CDP, which is display-independent, so it never intrudes on the user's
screen. When an agent needs a human to do a manual step (login / CAPTCHA / OTP /
2FA / consent), ``begin_takeover`` starts an on-demand noVNC bridge (x11vnc +
websockify) onto that team's Xvfb and returns a ``http://127.0.0.1:<port>/...``
URL the human opens in any browser, from this machine OR remotely via an SSH
port-forward. ``end_takeover`` tears the bridge down again. This is the only
design that works on every host, including headless / SSH boxes with no seat.

This connects via Hermes' CDP-override path (``browser.cdp_url``), which takes
precedence over both the cloud provider and the local launcher — so it works
without cloud credentials and reuses the Playwright Chromium we install locally.
"""

import glob
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

from swarm_server.config import WORKSPACE_ROOT

log = logging.getLogger("swarm.browser")

# Base port for per-team CDP endpoints; each team gets the next free port up.
_BASE_CDP_PORT = 9333
# Port bases for the on-demand takeover VNC bridge (one stack per active takeover).
_BASE_VNC_PORT = 5900   # x11vnc RFB port
_BASE_WEB_PORT = 6080   # websockify/noVNC web port (what the human opens)
# noVNC web assets shipped by the distro 'novnc' package.
_NOVNC_WEB = "/usr/share/novnc"
# Hidden Xvfb virtual screen geometry — also the size the human sees over VNC,
# and the browser window is sized to match so it fills that view exactly.
_SCREEN_W, _SCREEN_H = 1440, 900


def _find_chromium() -> Optional[str]:
    """Locate a Chromium executable from the Playwright browser cache.

    Prefers the full "Chrome for Testing" build (best site compatibility +
    persistent profile support); falls back to the lighter headless-shell.
    """
    roots = []
    pbp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if pbp:
        roots.append(Path(pbp))
    roots += [
        Path.home() / "Library" / "Caches" / "ms-playwright",   # macOS
        Path.home() / ".cache" / "ms-playwright",                # Linux
    ]
    patterns = [
        "chromium-*/chrome-mac*/*.app/Contents/MacOS/*",         # mac full chromium
        "chromium-*/chrome-linux*/chrome",                       # linux full chromium
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    ]
    for root in roots:
        for pat in patterns:
            for hit in sorted(glob.glob(str(root / pat)), reverse=True):
                if os.path.isfile(hit) and os.access(hit, os.X_OK):
                    return hit
    return None


class TeamBrowserManager:
    """Launches and tracks one persistent Chrome per team (on a hidden Xvfb)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # team_id -> {"proc": Popen, "port": int, "profile": str, "display": str}
        self._browsers: Dict[str, dict] = {}
        self._ports: Dict[str, int] = {}
        # team_id -> the dedicated hidden display its browser renders on for life.
        self._team_disp: Dict[str, str] = {}
        # team_id -> the Xvfb Popen backing that display.
        self._xvfb: Dict[str, subprocess.Popen] = {}
        # team_id -> active takeover VNC bridge:
        #   {"x11vnc": Popen, "web": Popen, "vnc_port": int, "web_port": int, "url": str}
        self._vnc: Dict[str, dict] = {}
        # teams currently handed to a human (so a relaunch keeps the bridge logic sane).
        self._takeover_active: set = set()
        self._chromium = _find_chromium()
        if self._chromium:
            log.info("Team browser pool using chromium: %s", self._chromium)
        else:
            log.warning(
                "No Chromium found for team browser pool "
                "(install with: npx playwright install chromium). "
                "Team browsers disabled."
            )

    # -- port helpers -------------------------------------------------------
    @staticmethod
    def _port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) != 0

    def _free_port(self, base: int, used: set) -> int:
        """First port >= base that is neither in `used` nor already bound."""
        port = base
        while port in used or not self._port_free(port):
            port += 1
        return port

    def _assign_port(self, team_id: str) -> int:
        if team_id in self._ports:
            return self._ports[team_id]
        port = self._free_port(_BASE_CDP_PORT, set(self._ports.values()))
        self._ports[team_id] = port
        return port

    def _healthy(self, port: int) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2
            ) as r:
                return r.status == 200
        except Exception:
            return False

    # -- display: a dedicated hidden Xvfb per team --------------------------
    @staticmethod
    def _display_socket_exists(disp: str) -> bool:
        """True if an X server is listening on a DISPLAY like ':100' (socket present)."""
        try:
            num = (disp or "").strip().lstrip(":").split(".")[0]
            return bool(num) and os.path.exists(f"/tmp/.X11-unix/X{num}")
        except Exception:
            return False

    def _ensure_team_xvfb(self, team_id: str) -> str:
        """Allocate (once) and keep alive a dedicated hidden Xvfb display for this
        team, returning its DISPLAY string. The browser renders here invisibly;
        a human only ever sees it through the on-demand takeover VNC bridge.

        Fool-proof on any host:
          - SWARM_BROWSER_DISPLAY override wins (explicit escape hatch),
          - otherwise a private Xvfb (works headless / over SSH / on Wayland),
          - if Xvfb itself is somehow missing, fall back to $DISPLAY/:0 so the
            agent still has *a* browser (it just won't be hidden).
        """
        with self._lock:
            disp = self._team_disp.get(team_id)
            proc = self._xvfb.get(team_id)
        if disp and (
            disp == os.environ.get("SWARM_BROWSER_DISPLAY", "").strip()
            or (proc is not None and proc.poll() is None and self._display_socket_exists(disp))
        ):
            return disp

        override = os.environ.get("SWARM_BROWSER_DISPLAY", "").strip()
        if override:
            with self._lock:
                self._team_disp[team_id] = override
            log.info("[%s] Browser display pinned to %s (override)", team_id, override)
            return override

        xvfb = shutil.which("Xvfb")
        if not xvfb:
            fallback = os.environ.get("DISPLAY") or ":0"
            log.warning("[%s] Xvfb unavailable; falling back to %s "
                        "(browser will be visible on this desktop)", team_id, fallback)
            with self._lock:
                self._team_disp[team_id] = fallback
            return fallback

        for num in range(100, 400):
            if os.path.exists(f"/tmp/.X11-unix/X{num}"):
                continue
            disp = f":{num}"
            try:
                p = subprocess.Popen(
                    [xvfb, disp, "-screen", "0", f"{_SCREEN_W}x{_SCREEN_H}x24",
                     "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                log.error("[%s] Failed to start Xvfb: %s", team_id, e)
                break
            ok = False
            for _ in range(40):
                if os.path.exists(f"/tmp/.X11-unix/X{num}"):
                    ok = True
                    break
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            if ok:
                with self._lock:
                    self._xvfb[team_id] = p
                    self._team_disp[team_id] = disp
                log.info("[%s] Hidden Xvfb display ready: %s (%dx%d)",
                         team_id, disp, _SCREEN_W, _SCREEN_H)
                return disp
            self._terminate(p)

        fallback = os.environ.get("DISPLAY") or ":0"
        log.warning("[%s] Could not allocate an Xvfb display; falling back to %s",
                    team_id, fallback)
        with self._lock:
            self._team_disp[team_id] = fallback
        return fallback

    # -- human takeover: on-demand noVNC bridge onto the team's hidden Xvfb --
    def _start_vnc(self, team_id: str, display: str) -> Optional[str]:
        """Start x11vnc + websockify/noVNC for the team's Xvfb and return the URL
        a human opens to see and operate the browser. Idempotent per team; bound
        to 127.0.0.1 only (reach it remotely via an SSH port-forward)."""
        with self._lock:
            existing = self._vnc.get(team_id)
        if existing and existing["x11vnc"].poll() is None and existing["web"].poll() is None:
            return existing["url"]
        if existing:  # half-dead bridge — clean it up and start fresh
            self._stop_vnc(team_id)

        x11vnc = shutil.which("x11vnc")
        websockify = shutil.which("websockify")
        if not x11vnc or not websockify:
            log.error("[%s] takeover needs x11vnc + websockify but one is missing; "
                      "cannot present the browser to the human", team_id)
            return None

        with self._lock:
            used_v = {v["vnc_port"] for v in self._vnc.values()}
            used_w = {v["web_port"] for v in self._vnc.values()}
        vnc_port = self._free_port(_BASE_VNC_PORT, used_v)
        web_port = self._free_port(_BASE_WEB_PORT, used_w)

        # x11vnc refuses to start if it thinks it's in a Wayland session (it
        # checks env vars BEFORE looking at the target X display). We always
        # target a real Xvfb X11 display, so scrub the Wayland hints and force
        # an X11 session type — otherwise it exits with "Wayland sessions ...".
        vnc_env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
        vnc_env["XDG_SESSION_TYPE"] = "x11"
        vnc_env["DISPLAY"] = display
        try:
            vproc = subprocess.Popen(
                [x11vnc, "-display", display, "-rfbport", str(vnc_port),
                 "-localhost", "-nopw", "-forever", "-shared", "-noxdamage", "-quiet"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=vnc_env,
            )
        except Exception as e:
            log.error("[%s] Failed to start x11vnc: %s", team_id, e)
            return None

        web_root = _NOVNC_WEB if os.path.isdir(_NOVNC_WEB) else None
        ws_cmd = [websockify]
        if web_root:
            ws_cmd += ["--web", web_root]
        ws_cmd += [f"127.0.0.1:{web_port}", f"127.0.0.1:{vnc_port}"]
        try:
            wproc = subprocess.Popen(
                ws_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            log.error("[%s] Failed to start websockify: %s", team_id, e)
            self._terminate(vproc)
            return None

        url = (f"http://127.0.0.1:{web_port}/vnc.html"
               f"?autoconnect=1&resize=remote&reconnect=1&path=websockify")
        with self._lock:
            self._vnc[team_id] = {
                "x11vnc": vproc, "web": wproc,
                "vnc_port": vnc_port, "web_port": web_port, "url": url,
            }
        # Wait briefly for the web port to start listening so the URL works
        # the instant the human clicks it.
        for _ in range(40):
            if not self._port_free(web_port) and not self._port_free(vnc_port):
                break
            if vproc.poll() is not None or wproc.poll() is not None:
                log.error("[%s] VNC bridge process died during startup", team_id)
                self._stop_vnc(team_id)
                return None
            time.sleep(0.1)
        log.info("[%s] Takeover VNC up: %s (x11vnc rfb=%d display=%s)",
                 team_id, url, vnc_port, display)
        return url

    def _stop_vnc(self, team_id: str) -> None:
        """Tear down the takeover VNC bridge; the browser keeps running on Xvfb."""
        with self._lock:
            v = self._vnc.pop(team_id, None)
        if not v:
            return
        for key in ("web", "x11vnc"):
            try:
                self._terminate(v[key])
            except Exception:
                pass
        log.info("[%s] Takeover VNC stopped", team_id)

    def begin_takeover(self, team_id: str, display: Optional[str] = None) -> Optional[str]:
        """Hand the team browser to a human. Ensures it is running on its hidden
        Xvfb, then starts an on-demand noVNC bridge and returns the URL the human
        opens to operate it. The browser never appears on the host desktop — only
        inside this VNC view. Returns None if the VNC stack can't start (the
        caller should tell the human so they aren't left waiting). `display` is
        accepted for backward-compat and ignored."""
        self.ensure_team_browser(team_id)
        with self._lock:
            self._takeover_active.add(team_id)
            disp = self._team_disp.get(team_id)
        if not disp:
            disp = self._ensure_team_xvfb(team_id)
        return self._start_vnc(team_id, disp)

    def end_takeover(self, team_id: str) -> Optional[str]:
        """Hand control back: tear down the VNC bridge. The browser keeps running
        invisibly on its Xvfb and the agent's CDP session is untouched. Returns
        the (unchanged) CDP URL."""
        with self._lock:
            self._takeover_active.discard(team_id)
            info = self._browsers.get(team_id)
        self._stop_vnc(team_id)
        return f"http://127.0.0.1:{info['port']}" if info else None

    def takeover_url(self, team_id: str) -> Optional[str]:
        """Current noVNC URL for an active takeover, or None."""
        with self._lock:
            v = self._vnc.get(team_id)
        return v["url"] if v else None

    # -- lifecycle ----------------------------------------------------------
    def ensure_team_browser(self, team_id: str) -> Optional[str]:
        """Return the team's CDP URL, launching/healing the browser as needed.

        Idempotent and cheap on the happy path (one HTTP health probe). Returns
        None when no Chromium is available so callers fall back gracefully.
        """
        if not self._chromium:
            return None
        with self._lock:
            info = self._browsers.get(team_id)
            if info and info["proc"].poll() is None and self._healthy(info["port"]):
                return f"http://127.0.0.1:{info['port']}"

            display = self._ensure_team_xvfb(team_id)

            # Reuse the team's port across relaunches so the cdp_url written into
            # agent configs stays valid within a server run.
            port = info["port"] if info else self._assign_port(team_id)

            # Reap a dead/stale process holding this slot.
            if info and info["proc"].poll() is None:
                self._terminate(info["proc"])

            profile = WORKSPACE_ROOT / team_id / ".browser-profile"
            profile.mkdir(parents=True, exist_ok=True)
            # A stale lock from an unclean shutdown blocks relaunch; clear it.
            for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                try:
                    (profile / lock_name).unlink()
                except OSError:
                    pass

            # Headful (NO --headless) so the browser passes headless fingerprinting
            # (webdriver=false via AutomationControlled off). It renders on the
            # team's hidden Xvfb and fills the virtual screen so a takeover VNC
            # view shows the whole browser. The anti-throttle flags keep it
            # painting even with no client attached, so agent screenshots work.
            args = [
                self._chromium,
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling",
                "--window-position=0,0",
                f"--window-size={_SCREEN_W},{_SCREEN_H}",
                "about:blank",
            ]
            try:
                # start_new_session=True puts Chrome (and its renderer/gpu
                # helper children) in its own process group so we can reap the
                # WHOLE tree on shutdown instead of orphaning helpers.
                proc = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env={**os.environ, "DISPLAY": display},
                )
            except Exception as e:
                log.error("[%s] Failed to launch team browser: %s", team_id, e)
                return None

            self._browsers[team_id] = {
                "proc": proc, "port": port, "profile": str(profile),
                "display": display,
            }
            self._ports[team_id] = port

            # Wait (≤10s) for the CDP endpoint to come up.
            ready = False
            for _ in range(50):
                if proc.poll() is not None:
                    log.error("[%s] Team browser exited during startup", team_id)
                    return None
                if self._healthy(port):
                    log.info("[%s] Team browser ready on port %d display=%s (profile=%s)",
                             team_id, port, display, profile)
                    ready = True
                    break
                time.sleep(0.2)
            if not ready:
                log.error("[%s] Team browser did not become healthy on port %d", team_id, port)
                return None

        return f"http://127.0.0.1:{port}"

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """Terminate a process and its whole process group (helpers/children)."""
        if proc is None or proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                if pgid is not None:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except Exception:
                        pass
                else:
                    proc.kill()
        except Exception:
            pass

    def shutdown_all(self) -> None:
        """Terminate all team browsers, VNC bridges and Xvfb displays (profiles
        persist on disk for next run)."""
        with self._lock:
            for team_id in list(self._vnc.keys()):
                self._stop_vnc(team_id)
            for team_id, info in self._browsers.items():
                log.info("[%s] Stopping team browser (port %d)", team_id, info["port"])
                self._terminate(info["proc"])
            self._browsers.clear()
            for team_id, proc in self._xvfb.items():
                self._terminate(proc)
            self._xvfb.clear()


# Process-wide singleton.
team_browser_manager = TeamBrowserManager()
