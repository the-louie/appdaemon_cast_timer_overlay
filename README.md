# appdaemon_cast_timer_overlay

Keeps a photo slideshow (for example Immich Kiosk) on a Google Nest Hub, and shows
the Hub's own voice timers as a countdown in a corner on top of the photos.

Without it, a voice timer drops the cast, and a blind re-cast job later covers the
timer with photos.

## What it does

- Serves an overlay page from AppDaemon's web server at `<appdaemon_url>/app/<route>`:
  the slideshow in a full-screen iframe plus a countdown chip. The page polls the
  same route (`?format=json`) for timers. No Home Assistant token reaches the Hub.
- Casts that page to the Hub with DashCast whenever the Hub's cast entity has been
  `off` for `settle_s` seconds.
- Does not cast while a timer rings (or is due), or while the Hub plays anything else.

## Requirements

- AppDaemon 4.5 with its web server enabled (`http:` in `appdaemon.yaml`).
- The Google Home custom integration (HACS, leikoilja/ha-google-home), which provides
  `sensor.<device>_timers`. If Home Assistant runs in Docker without host networking,
  add the Hub's IP under the integration's known hosts.
- `pychromecast`, from `requirements.txt`. The AppDaemon Docker image installs any
  `requirements.txt` under `/conf` when the container starts, so restart the container once.
- The Hub can reach `appdaemon_url` and `kiosk_url`. The Hub can frame `kiosk_url`.

Stop any other job that re-casts to the same Hub, or the two will fight.

## Configuration

See `config.yaml.example`.
