"""Cast timer overlay — a photo frame on a Nest Hub that shows the Hub's own timers.

Why this exists
---------------
A Nest Hub casting a photo slideshow (Immich Kiosk, or any web page) drops the
cast the moment someone sets a voice timer. A blind re-cast then brings the
photos back over the timer, and nobody can see how long is left. This app
replaces the blind re-cast with one that knows about timers, and draws the
countdown itself, in a corner on top of the photos.

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

import hashlib
import html
import math
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


# -- info box: weather, price, battery, solar balance (2026-11-12) ------------

INFO_ENTITIES = ("temperature_sensor", "weather_entity", "price_sensor",
                 "battery_sensor", "solar_power_sensor", "house_power_sensor")

# Share of today's price slots: the current price is "cheap" when fewer than 20 %
# of them are cheaper, "dear" when fewer than 10 % are dearer (owner, 2026-11-12).
CHEAP_SHARE = 0.20
DEAR_SHARE = 0.10


def to_float(value) -> float | None:
    """A sensor state as a number; None for unavailable, unknown or junk."""
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def price_level(current: float | None, today) -> str | None:
    """cheap | mid | dear for the current price against today's prices."""
    if current is None or not isinstance(today, list):
        return None
    prices = [p for p in (to_float(x) for x in today) if p is not None]
    if not prices:
        return None
    n = len(prices)
    if sum(p < current for p in prices) < CHEAP_SHARE * n:
        return "cheap"
    if sum(p > current for p in prices) < DEAR_SHARE * n:
        return "dear"
    return "mid"


def power_kw(value, unit) -> float | None:
    """A power sensor's state in kW, from W or kW."""
    v = to_float(value)
    if v is None:
        return None
    return v / 1000 if str(unit or "W").strip() == "W" else v


def build_info(temp=None, condition=None, price=None, today_prices=None, battery=None,
               solar_kw=None, house_kw=None) -> dict:
    """What the page's info box gets. A missing value is left out, never guessed."""
    info: dict = {}
    t = to_float(temp)
    if t is not None:
        info["temp"] = round(t, 1)
    if isinstance(condition, str) and condition not in ("unavailable", "unknown", ""):
        info["condition"] = condition
    p = to_float(price)
    if p is not None:
        info["price"] = round(p, 2)
        level = price_level(p, today_prices)
        if level:
            info["level"] = level
    b = to_float(battery)
    if b is not None:
        info["battery"] = round(b)
    if solar_kw is not None and house_kw is not None:
        info["delta_kw"] = round(solar_kw - house_kw, 1)
    return info


def render_page(kiosk_url: str, corner: str, poll_s: int,
                info_corner: str = "bottom-right") -> str:
    """The overlay page. Config values are escaped for the context they land in.

    The page carries a hash of itself; the JSON poll returns the hash of the page
    AppDaemon serves now, so a Hub showing an older page reloads it.
    """
    page = PAGE_TEMPLATE.format(
        kiosk_url=html.escape(kiosk_url, quote=True),
        corner_css=CORNERS[corner],
        info_corner_css=CORNERS[info_corner],
        poll_ms=json.dumps(int(poll_s * 1000)),
        icons_js=ICONS_JS,
        page_version=_VERSION_SLOT,
    )
    return page.replace(_VERSION_SLOT, hashlib.sha1(page.encode()).hexdigest()[:12])


_VERSION_SLOT = "@@page-version@@"


def page_version(page: str) -> str:
    """The hash render_page() put into the page."""
    return page.split('var PAGE_VERSION = "', 1)[1].split('"', 1)[0]


# Weather icons (48x48 viewBox) keyed by Home Assistant's weather conditions.
# Inline, so the Hub needs no internet for them. Passed into PAGE_TEMPLATE as a
# value, so its braces are not format fields.
ICONS_JS = r"""
var SUN='<circle cx="24" cy="24" r="9" fill="#FFC83D"/><g stroke="#FFC83D" stroke-width="3" stroke-linecap="round"><path d="M24 5v5M24 38v5M5 24h5M38 24h5M10.6 10.6l3.5 3.5M33.9 33.9l3.5 3.5M10.6 37.4l3.5-3.5M33.9 14.1l3.5-3.5"/></g>';
var MOON='<path d="M30 8a16 16 0 1 0 10 26A13 13 0 0 1 30 8z" fill="#E8E4C9"/>';
function CLOUD(y,c){return '<path d="M14 '+(36+y)+'h21a8 8 0 0 0 0-16 11 11 0 0 0-21-2 8 8 0 0 0 0 18z" fill="'+(c||'#F2F4F7')+'"/>';}
var DROPS='<g stroke="#5AB4FF" stroke-width="3" stroke-linecap="round"><path d="M17 40l-2 5M25 40l-2 5M33 40l-2 5"/></g>';
var FLAKES='<g fill="#FFFFFF"><circle cx="16" cy="42" r="2.4"/><circle cx="24" cy="45" r="2.4"/><circle cx="32" cy="42" r="2.4"/></g>';
var BOLT='<path d="M25 37l-5 9h5l-2 7 8-11h-5l3-5z" fill="#FFC83D"/>';
var FOG='<g stroke="#D7DCE3" stroke-width="3" stroke-linecap="round"><path d="M9 20h30M6 27h36M9 34h30"/></g>';
var WIND='<g stroke="#D7DCE3" stroke-width="3" stroke-linecap="round" fill="none"><path d="M6 18h24a5 5 0 1 0-5-5M6 26h32a5 5 0 1 1-5 5M6 34h16"/></g>';
var ICONS={
 'sunny':SUN, 'clear-night':MOON,
 'partlycloudy':'<g transform="translate(6,-6) scale(.8)">'+SUN+'</g>'+CLOUD(4),
 'cloudy':CLOUD(-2,'#C9CFD8').replace('M14','M10')+CLOUD(4),
 'rainy':CLOUD(-6)+DROPS,
 'pouring':CLOUD(-6,'#AEB6C2')+DROPS.replace('stroke-width="3"','stroke-width="4"'),
 'snowy':CLOUD(-6)+FLAKES,
 'snowy-rainy':CLOUD(-6)+'<g stroke="#5AB4FF" stroke-width="3" stroke-linecap="round"><path d="M17 40l-2 5M31 40l-2 5"/></g><circle cx="24" cy="43" r="2.4" fill="#fff"/>',
 'hail':CLOUD(-6)+FLAKES,
 'lightning':CLOUD(-8,'#AEB6C2')+BOLT,
 'lightning-rainy':CLOUD(-8,'#AEB6C2')+BOLT+DROPS.replace('M25 40l-2 5',''),
 'fog':FOG, 'windy':WIND, 'windy-variant':CLOUD(-8)+WIND,
 'exceptional':'<path d="M24 6l20 36H4z" fill="#FFC83D"/><path d="M24 18v12M24 35v2" stroke="#000" stroke-width="3.5" stroke-linecap="round"/>'
};
function icon(c){var s=ICONS[c];return s?'<svg viewBox="0 0 48 48" width="58" height="58">'+s+'</svg>':'';}
// Small row-2 icons; currentColor, so they take the price/kW colour.
var BOLT_ICON='<svg width="14" height="20" viewBox="0 0 14 20"><path d="M9 0L0 12h6l-2 8 10-13H8z" fill="currentColor"/></svg>';
var PANEL_ICON='<svg width="24" height="20" viewBox="0 0 24 20"><path d="M5 2h14l4 12H1z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M8.3 2L7 14M15.7 2L17 14M3 8h18" stroke="currentColor" stroke-width="1.6"/><path d="M12 14v4M8 18h8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
function batteryIcon(b){var w=Math.max(0,Math.min(16,Math.round(16*b/100)));
 return '<svg width="26" height="14" viewBox="0 0 26 14"><rect x="1" y="1" width="21" height="12" rx="3" fill="none" stroke="#fff" stroke-width="2"/><rect x="23" y="4.5" width="2.5" height="5" rx="1" fill="#fff"/><rect x="3.5" y="3.5" width="'+w+'" height="7" rx="1.5" fill="#fff"/></svg>';}
"""


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
#info{{position:fixed;{info_corner_css};display:none;flex-direction:column;z-index:10;
  background:rgba(0,0,0,.62);color:#fff;border-radius:16px;padding:8px 18px 10px 14px;
  font-family:Roboto,Arial,sans-serif;font-variant-numeric:tabular-nums;line-height:1.1}}
#info .row1{{display:flex;align-items:center;justify-content:space-between;gap:8px}}
#info .temp{{font-size:44px;font-weight:600;white-space:nowrap}}
#info .row2{{display:flex;justify-content:flex-end;align-items:center;gap:10px;
  font-size:24px;font-weight:500;white-space:nowrap;margin-top:2px}}
#info .bat{{display:inline-flex;align-items:center;gap:4px;opacity:.9}}
#info .kw{{opacity:.9}}
#info .ic{{display:inline-flex;align-items:center;gap:5px}}
.cheap{{color:#7CE38B}}.mid{{color:#FFD54A}}.dear{{color:#FF7A6E}}
#info .kw.pos{{color:#7CE38B;opacity:1}}
</style></head><body>
<iframe src="{kiosk_url}" allow="autoplay; fullscreen"></iframe>
<div id="timers"></div>
<div id="info"></div>
<script>{icons_js}</script>
<script>
var POLL_MS = {poll_ms}, STALE_S = 60;
var PAGE_VERSION = "{page_version}";
var timers = [], info = {{}}, offset = 0, lastOk = 0;
function num(v, d) {{ return v.toFixed(d).replace(".", ",").replace("-", "\u2212"); }}
function renderInfo(stale) {{
  var box = document.getElementById("info"), h = "", r2 = [];
  if (stale || !Object.keys(info).length) {{ box.style.display = "none"; return; }}
  if (info.temp !== undefined || info.condition) {{
    h += '<div class="row1">' + (info.condition ? icon(info.condition) : "") +
      (info.temp !== undefined ? '<span class="temp">' + num(info.temp, 1) + " \u00B0C</span>" : "") +
      "</div>";
  }}
  if (info.price !== undefined)
    r2.push('<span class="ic ' + (info.level || "") + '">' + BOLT_ICON + num(info.price, 2) + " kr</span>");
  if (info.battery !== undefined)
    r2.push('<span class="bat">' + batteryIcon(info.battery) + info.battery + " %</span>");
  if (info.delta_kw !== undefined)
    r2.push('<span class="ic kw' + (info.delta_kw > 0 ? " pos" : "") + '">' + PANEL_ICON +
      (info.delta_kw >= 0 ? "+" : "") + num(info.delta_kw, 1) + " kW</span>");
  if (r2.length) h += '<div class="row2">' + r2.join("") + "</div>";
  box.innerHTML = h;
  box.style.display = "flex";
  fitRow1(box);
}}
// Icon + temperature as wide as the row below (owner 2026-11-12): scale both.
function fitRow1(box) {{
  var r1 = box.querySelector(".row1"), r2 = box.querySelector(".row2");
  if (!r1 || !r2) return;
  var temp = r1.querySelector(".temp"), svg = r1.querySelector("svg");
  var target = r2.scrollWidth, size = 44;
  for (var i = 0; i < 4; i++) {{
    if (temp) temp.style.fontSize = size + "px";
    if (svg) {{ svg.setAttribute("width", Math.round(size * 1.3)); svg.setAttribute("height", Math.round(size * 1.3)); }}
    r1.style.width = "auto";
    var w = r1.scrollWidth;
    if (!w || Math.abs(w - target) < 2) break;
    size = Math.max(32, Math.min(80, size * target / w));
  }}
  r1.style.width = target + "px";
}}
function fmt(s) {{
  s = Math.max(0, Math.ceil(s));
  var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  var mm = h ? (m < 10 ? "0" + m : "" + m) : "" + m;
  return (h ? h + ":" : "") + mm + ":" + (sec < 10 ? "0" + sec : sec);
}}
function poll() {{
  fetch("?format=json", {{cache: "no-store"}}).then(function (r) {{ return r.json(); }})
    .then(function (d) {{
      // AppDaemon serves a newer page (app updated): load it.
      if (d.page && d.page !== PAGE_VERSION) {{ location.reload(); return; }}
      timers = d.timers || [];
      info = d.info || {{}};
      offset = d.now - Date.now() / 1000;
      lastOk = Date.now() / 1000;
    }}).catch(function () {{}});
}}
function render() {{
  var box = document.getElementById("timers");
  var local = Date.now() / 1000, now = local + offset;
  box.textContent = "";
  renderInfo(local - lastOk > STALE_S);
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
        self.info_corner = a.get("info_corner", "bottom-right")
        if self.info_corner not in CORNERS or self.info_corner == self.corner:
            raise ValueError(f"info_corner must be one of {sorted(CORNERS)} and differ from "
                             f"corner, got {self.info_corner!r}")
        # Optional; each one that is set must exist.
        self.info_entities = {k: self._require_entity(k) for k in INFO_ENTITIES
                              if self.args.get(k)}

        self._page = render_page(self.kiosk_url, self.corner, self.page_poll_s,
                                 self.info_corner)
        self._page_version = page_version(self._page)
        self._info: dict = {}
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
        if self.info_entities:
            for entity in self.info_entities.values():
                self.listen_state(self._on_info, entity)
            self._on_info()  # first page load must not wait for the timer below
            # Nordpool's day list changes at midnight without a state change.
            self.run_every(self._on_info, "now", 60)
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

    def _on_info(self, *args, **kwargs) -> None:
        """Refresh the info box cache. The web route is async and must not call get_state."""
        e = self.info_entities

        def state(key, attribute=None):
            return self.get_state(e[key], attribute=attribute) if key in e else None

        def kw(key):
            if key not in e:
                return None
            return power_kw(state(key), state(key, "unit_of_measurement"))

        self._info = build_info(
            temp=state("temperature_sensor"),
            condition=state("weather_entity"),
            price=state("price_sensor"),
            today_prices=state("price_sensor", "today"),
            battery=state("battery_sensor"),
            solar_kw=kw("solar_power_sensor"),
            house_kw=kw("house_power_sensor"),
        )

    def _tick(self, kwargs) -> None:
        # Epoch seconds, like the sensor's fire_time. Not get_now(): this module has an
        # async route, and tests/test_async_app_clock.py bans get_now().attr here.
        now = time.time()
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
            body = json.dumps({"now": now, "timers": page_timers(self._timers, now),
                               "info": self._info, "page": self._page_version})
            return web.Response(text=body, content_type="application/json", headers=headers)
        return web.Response(text=self._page, content_type="text/html", headers=headers)
