"""Cast timer overlay — a photo frame on a Nest Hub that shows the Hub's own timers.

Why this exists
---------------
A Nest Hub casting a photo slideshow (Immich Kiosk, or any web page) drops the
cast the moment someone sets a voice timer. A blind re-cast then brings the
photos back over the timer, and nobody can see how long is left. This app
replaces the blind re-cast with one that knows about timers, and draws the
countdown itself, in a corner on top of the photos.

Design and owner decisions: `docs/projects/nest-hub-timer-overlay.md` in
/code/hass.

How it works
------------
1. The app serves one page from AppDaemon's web server, at
   ``http://<appdaemon>:5050/app/<route>``: the slideshow in a full-screen
   iframe, plus a countdown chip. The page polls ``?format=json`` on the same
   route for the timers; no Home Assistant token ever reaches the Hub.
2. Timers come from the Google Home custom integration (leikoilja/ha-google-home),
   ``sensor.<device>_timers``. It polls the Hub every 15 s.
3. The app casts that page to the Hub with DashCast, directly over pychromecast,
   because Home Assistant's ``media_player.play_media`` cannot start DashCast
   (pychromecast's ``quick_play`` has no DashCast entry, checked in 14.0.10).

When it casts
-------------
When the Hub's cast entity is ``off`` (ambient screen, clock, or a timer
screen) and has been for ``settle_s``, so an Assistant answer is not cut short.
Also, at once, when DashCast is showing a page this app did not cast: Home
Assistant does not report DashCast's URL, and on 2026-11-02 a plain Kiosk cast
from an older job stayed on screen through a voice timer, hiding it.
Never while a timer rings: measured 2026-11-02, the ringing screen drops the
cast 2 s after the timer's fire time, but the sensor shows ``ringing`` only at
its next poll, 8 s later. So a ``set`` timer whose fire time has passed counts as
ringing too. Never over anything else the Hub is playing (YouTube, music).
"""

import html
import json
import threading
import time
from dataclasses import dataclass

import appdaemon.plugins.hass.hassapi as hass

from ha_states import is_reporting

DASHCAST_APP_NAME = "DashCast"

# A `set` timer whose fire time passed this long ago is a stale sensor, not a
# ringing timer. Google stops ringing on its own after about 10 minutes.
RING_WINDOW_S = 600

# Our own force-launch makes the player change state for a few seconds. Changes
# inside this window after a cast are ours and do not mean someone else took over.
OWN_CAST_GRACE_S = 30

CORNERS = {
    "top-right": "top:16px;right:16px;align-items:flex-end",
    "top-left": "top:16px;left:16px;align-items:flex-start",
    "bottom-right": "bottom:16px;right:16px;align-items:flex-end",
    "bottom-left": "bottom:16px;left:16px;align-items:flex-start",
}


@dataclass(frozen=True)
class Timer:
    status: str             # set | ringing | paused
    fire_time: float | None  # unix seconds; None while paused
    remaining_s: int | None  # paused timers only
    label: str | None


def parse_hms(value) -> int | None:
    """'0:10:00' -> 600. None for anything else."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        return None
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def parse_timers(raw) -> list[Timer]:
    """The integration's `timers` attribute, as Timers. Malformed entries are skipped."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status not in ("set", "ringing", "paused"):
            continue
        fire = item.get("fire_time")
        fire = float(fire) if isinstance(fire, (int, float)) else None
        label = item.get("label")
        out.append(Timer(
            status=status,
            fire_time=fire,
            remaining_s=parse_hms(item.get("remaining")),
            label=label if isinstance(label, str) and label else None,
        ))
    return out


def is_ringing(timer: Timer, now: float) -> bool:
    """Ringing, or due: the screen belongs to the Hub's alarm UI."""
    if timer.status == "ringing":
        return True
    return (timer.status == "set" and timer.fire_time is not None
            and 0 <= now - timer.fire_time <= RING_WINDOW_S)


def decide(player_state, app_name, timers: list[Timer], now: float,
           off_for_s: float | None, settle_s: float, ours: bool) -> tuple[bool, str]:
    """Whether to cast now, and why. Pure, so the rules can be tested."""
    if not is_reporting(player_state):
        return False, f"player {player_state}"
    if any(is_ringing(t, now) for t in timers):
        return False, "timer ringing"
    if player_state != "off":
        if app_name == DASHCAST_APP_NAME:
            return (False, "already showing") if ours else (True, "taking over DashCast")
        return False, f"player busy ({app_name or player_state})"
    if off_for_s is None or off_for_s < settle_s:
        return False, "settling"
    return True, "screen free"


def page_timers(timers: list[Timer], now: float) -> list[dict]:
    """What the page needs, in display order. Stale `set` timers are dropped."""
    out = []
    for t in timers:
        if t.status == "set" and t.fire_time is not None and now - t.fire_time > RING_WINDOW_S:
            continue
        out.append({"status": t.status, "fire_time": t.fire_time,
                    "remaining_s": t.remaining_s, "label": t.label})
    return out


def render_page(kiosk_url: str, corner: str, poll_s: int) -> str:
    """The overlay page. Config values are escaped for the context they land in."""
    return PAGE_TEMPLATE.format(
        kiosk_url=html.escape(kiosk_url, quote=True),
        corner_css=CORNERS[corner],
        poll_ms=json.dumps(int(poll_s * 1000)),
    )


PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photo frame</title>
<style>
html,body{{margin:0;height:100%;background:#000;overflow:hidden}}
iframe{{position:fixed;top:0;left:0;width:100%;height:100%;border:0}}
#timers{{position:fixed;{corner_css};display:none;flex-direction:column;gap:8px;
  font-family:Roboto,Arial,sans-serif;z-index:10}}
.t{{background:rgba(0,0,0,.62);color:#fff;border-radius:16px;padding:6px 18px;
  font-size:56px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.15}}
.t small{{display:block;font-size:20px;font-weight:400;opacity:.85}}
.t.done{{background:rgba(200,30,30,.88)}}
</style></head><body>
<iframe src="{kiosk_url}" allow="autoplay; fullscreen"></iframe>
<div id="timers"></div>
<script>
var POLL_MS = {poll_ms}, STALE_S = 60;
var timers = [], offset = 0, lastOk = 0;
function fmt(s) {{
  s = Math.max(0, Math.ceil(s));
  var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  var mm = h ? (m < 10 ? "0" + m : "" + m) : "" + m;
  return (h ? h + ":" : "") + mm + ":" + (sec < 10 ? "0" + sec : sec);
}}
function poll() {{
  fetch("?format=json", {{cache: "no-store"}}).then(function (r) {{ return r.json(); }})
    .then(function (d) {{
      timers = d.timers || [];
      offset = d.now - Date.now() / 1000;
      lastOk = Date.now() / 1000;
    }}).catch(function () {{}});
}}
function render() {{
  var box = document.getElementById("timers");
  var local = Date.now() / 1000, now = local + offset;
  box.textContent = "";
  if (local - lastOk > STALE_S || !timers.length) {{ box.style.display = "none"; return; }}
  timers.forEach(function (t) {{
    var left = t.status === "paused" ? (t.remaining_s || 0)
             : t.status === "ringing" ? 0 : (t.fire_time - now);
    var row = document.createElement("div");
    row.className = "t" + (left <= 0 ? " done" : "");
    row.textContent = (t.status === "paused" ? "\\u23F8 " : "") + fmt(left);
    if (t.label) {{
      var small = document.createElement("small");
      small.textContent = t.label;
      row.appendChild(small);
    }}
    box.appendChild(row);
  }});
  box.style.display = "flex";
}}
poll(); render();
setInterval(poll, POLL_MS);
setInterval(render, 250);
</script>
</body></html>
"""


class CastTimerOverlay(hass.Hass):

    def initialize(self) -> None:
        a = self.args
        self.media_player = self._require_entity("media_player")
        self.timers_sensor = self._require_entity("timers_sensor")
        self.cast_host = self._require_str("cast_host")
        self.kiosk_url = self._require_url("kiosk_url")
        base = self._require_url("appdaemon_url").rstrip("/")
        self.route = str(a.get("route", self.name))
        self.page_url = f"{base}/app/{self.route}"
        self.corner = a.get("corner", "top-right")
        if self.corner not in CORNERS:
            raise ValueError(f"corner must be one of {sorted(CORNERS)}, got {self.corner!r}")
        self.settle_s = self._require_number("settle_s", 20)
        self.min_cast_interval_s = self._require_number("min_cast_interval_s", 60)
        self.check_interval_s = int(self._require_number("check_interval_s", 10))
        self.page_poll_s = self._require_number("page_poll_s", 5)

        self._page = render_page(self.kiosk_url, self.corner, self.page_poll_s)
        self._timers: list[Timer] = parse_timers(
            self.get_state(self.timers_sensor, attribute="timers"))
        self._off_since: float | None = (
            time.monotonic() if self.get_state(self.media_player) == "off" else None)
        self._last_cast: float | None = None
        # True from our successful cast until the player leaves that DashCast session.
        self._ours = False
        self._cast_failures = 0
        self._cast_lock = threading.Lock()

        self.register_route(self._serve, self.route)
        self.listen_state(self._on_player, self.media_player)
        self.listen_state(self._on_timers, self.timers_sensor, attribute="timers")
        self.run_every(self._tick, "now", self.check_interval_s)
        self.log(f"serving {self.page_url}; casting to {self.cast_host} when "
                 f"{self.media_player} is free")

    # -- config ---------------------------------------------------------------

    def _require_entity(self, key: str) -> str:
        value = self.args.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"'{key}' is required")
        if not self.entity_exists(value):
            raise ValueError(f"'{key}': {value} does not exist (created after AppDaemon "
                             f"started? restart AppDaemon)")
        return value

    def _require_str(self, key: str) -> str:
        value = self.args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"'{key}' is required")
        return value.strip()

    def _require_url(self, key: str) -> str:
        value = self._require_str(key)
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"'{key}' must start with http:// or https://, got {value!r}")
        return value

    def _require_number(self, key: str, default: float) -> float:
        value = self.args.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"'{key}' must be a positive number, got {value!r}")
        return value

    # -- state ----------------------------------------------------------------

    def _on_player(self, entity, attribute, old, new, kwargs) -> None:
        own = self._last_cast is not None and time.monotonic() - self._last_cast < OWN_CAST_GRACE_S
        if new != old and not own:
            self._ours = False
        if new == "off" and old != "off":
            self._off_since = time.monotonic()
        elif new != "off":
            self._off_since = None
        self._tick({})

    def _on_timers(self, entity, attribute, old, new, kwargs) -> None:
        self._timers = parse_timers(new)
        self._tick({})

    def _tick(self, kwargs) -> None:
        now = self.get_now().timestamp()
        player = self.get_state(self.media_player)
        app_name = self.get_state(self.media_player, attribute="app_name")
        off_for = None if self._off_since is None else time.monotonic() - self._off_since
        cast, reason = decide(player, app_name, self._timers, now, off_for, self.settle_s,
                              self._ours)
        if not cast:
            return
        if self._last_cast is not None and time.monotonic() - self._last_cast < self.min_cast_interval_s:
            return
        self._cast(reason)

    # -- casting --------------------------------------------------------------

    def _cast(self, reason: str) -> None:
        if not self._cast_lock.acquire(blocking=False):
            return
        try:
            self._last_cast = time.monotonic()
            self.log(f"casting {self.page_url} ({reason})")
            self._dashcast(self.page_url)
            self._ours = True
            self._cast_failures = 0
        except Exception as e:
            # Network and pychromecast's own PyChromecastError family are runtime
            # failures; anything else is a programming error and must surface.
            if not (isinstance(e, OSError) or type(e).__module__.startswith("pychromecast")):
                raise
            self._cast_failures += 1
            level = "ERROR" if self._cast_failures >= 3 else "WARNING"
            self.log(f"cast to {self.cast_host} failed ({self._cast_failures} in a row): "
                     f"{type(e).__name__}: {e}", level=level)
        finally:
            self._cast_lock.release()

    def _dashcast(self, url: str) -> None:
        """Launch DashCast fresh and load `url` top-level, as catt's cast_site does.

        DashCast stops answering once a page is loaded with force=True, so the
        app is force-launched every time rather than reused. It never
        acknowledges a URL either (measured 2026-11-02), so success is not
        awaited; the URL is sent only once DashCast's namespace is up, because a
        message sent while it reads "Application is starting" is dropped.
        """
        import pychromecast
        from pychromecast.config import APP_DASHCAST
        from pychromecast.controllers.dashcast import DashCastController

        cast = pychromecast.get_chromecast_from_host(
            (self.cast_host, 8009, None, None, None), tries=1, timeout=10)
        try:
            cast.wait(timeout=15)
            controller = DashCastController()
            cast.register_handler(controller)
            cast.start_app(APP_DASHCAST, force_launch=True, timeout=15)
            deadline = time.monotonic() + 10
            while controller.namespace not in (cast.status.namespaces or []):
                if time.monotonic() > deadline:
                    raise TimeoutError("DashCast did not become ready within 10 s")
                time.sleep(0.25)
            controller.load_url(url, force=True)
            time.sleep(2)  # let the message leave before the socket closes
        finally:
            cast.disconnect(timeout=5)

    # -- web ------------------------------------------------------------------

    async def _serve(self, request, kwargs):
        from aiohttp import web

        headers = {"Cache-Control": "no-store"}
        if request.query.get("format") == "json":
            now = time.time()
            body = json.dumps({"now": now, "timers": page_timers(self._timers, now)})
            return web.Response(text=body, content_type="application/json", headers=headers)
        return web.Response(text=self._page, content_type="text/html", headers=headers)
