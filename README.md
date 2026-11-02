# appdaemon_cast_timer_overlay

A photo frame on a Google Nest Hub that doesn't hide your kitchen timers.

When you cast a slideshow (for example [Immich Kiosk](https://github.com/damongolding/immich-kiosk))
to a Nest Hub, a voice timer and the slideshow end up competing for the screen. Re-casting
on a schedule brings the photos back, but covers the timer, so you can't see how long is left.

This AppDaemon app keeps the slideshow on the Hub **and** shows the Hub's own timers
as a live countdown in a corner, on top of the photos.

## What it does

- **Overlay page.** The app serves a page from AppDaemon's web server. It shows your
  slideshow full-screen and puts one countdown per running timer in a corner, with the
  timer's name if it has one. Paused timers show ⏸.
- **Timer-aware casting.** The page is cast with DashCast whenever the Hub is free.
  The app never casts:
  - while a timer is ringing, so the Hub's own alarm screen stays up;
  - over anything else the Hub is playing (YouTube, music);
  - during the first `settle_s` seconds after the Hub drops the cast, so an Assistant
    answer is not cut off.
- **Self-healing.** If the cast drops, the photos come back once the screen is free. If
  another DashCast page takes over the screen, the overlay replaces it.

No Home Assistant token reaches the Hub: the page only gets timer data, from the app.

## Requirements

- **AppDaemon 4.5** with its web server enabled (`http:` in `appdaemon.yaml`).
- **[Google Home](https://github.com/leikoilja/ha-google-home)** custom integration
  (HACS), which provides `sensor.<device>_timers`. If Home Assistant runs in Docker
  without host networking, add the Hub's IP under the integration's *Known hosts*.
- **pychromecast**, from `requirements.txt`. The AppDaemon Docker image installs it
  when the container starts, so restart the container once.
- The Hub must be able to reach both `appdaemon_url` and `kiosk_url`, and the slideshow
  must allow being shown inside another page (no `X-Frame-Options: DENY`).
- A fixed IP address for the Hub.

Turn off any other job that casts to the same Hub, or the two will fight over the screen.

## Setup

1. Clone this repository into AppDaemon's `apps/` directory.
2. Copy `config.yaml.example` to `config.yaml` and fill in the entities, the Hub's IP,
   the slideshow URL, and AppDaemon's URL as the Hub sees it.
3. Restart AppDaemon. Open `<appdaemon_url>/app/<route>` in a browser to preview the page.

## Configuration

| Key | Required | Default | Meaning |
|---|---|---|---|
| `media_player` | yes | | The Hub's cast entity |
| `timers_sensor` | yes | | `sensor.<device>_timers` from the Google Home integration |
| `cast_host` | yes | | The Hub's IP address |
| `kiosk_url` | yes | | The slideshow URL |
| `appdaemon_url` | yes | | AppDaemon's web server as the Hub reaches it |
| `route` | | app name | The page is served at `/app/<route>` |
| `corner` | | `top-right` | `top-right`, `top-left`, `bottom-right`, `bottom-left` |
| `settle_s` | | `20` | Seconds the Hub must be free before casting |
| `min_cast_interval_s` | | `60` | Minimum time between cast attempts |
| `check_interval_s` | | `10` | How often the app re-checks the Hub |
| `page_poll_s` | | `5` | How often the page asks for timers |

## Limits

- Timers are read every 15 s by the Google Home integration, so a new timer can take up
  to 15 s to appear. Once it appears, the page counts down every second on its own.
- Home Assistant does not report which page DashCast is showing. The app therefore
  treats any DashCast session it did not start itself as someone else's, and replaces it.

## License

BSD-2-Clause
