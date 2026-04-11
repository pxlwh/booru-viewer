# Hyprland integration

I daily-drive booru-viewer on Hyprland and I've baked in my own opinions
on how the app should behave there. By default, a handful of `hyprctl`
dispatches run at runtime to:

- Restore the main window's last floating mode + dimensions on launch
- Restore the popout's position, center-pin it around its content during
  navigation, and suppress F11 / fullscreen-transition flicker
- "Prime" Hyprland's per-window floating cache at startup so a mid-session
  toggle to floating uses your saved dimensions
- Lock the popout's aspect ratio to its content so you can't accidentally
  stretch mpv playback by dragging the popout corner

## Opting out

If you're a ricer with your own `windowrule`s targeting
`class:^(booru-viewer)$` and you'd rather the app keep its hands off your
setup, there are two independent opt-out env vars:

- **`BOORU_VIEWER_NO_HYPR_RULES=1`** — disables every in-code hyprctl
  dispatch *except* the popout's `keep_aspect_ratio` lock. Use this if
  you want app-side window management out of the way but you still want
  the popout to size itself to its content.
- **`BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK=1`** — independently disables
  the popout's aspect ratio enforcement. Useful if you want to drag the
  popout to whatever shape you like (square, panoramic, monitor-aspect,
  whatever) and accept that mpv playback will letterbox or stretch to
  match.

For the full hands-off experience, set both:

```ini
[Desktop Entry]
Name=booru-viewer
Exec=env BOORU_VIEWER_NO_HYPR_RULES=1 BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK=1 /path/to/booru-viewer/.venv/bin/booru-viewer
Icon=/path/to/booru-viewer/icon.png
Type=Application
Categories=Graphics;
```

Or for one-off launches from a shell:

```bash
BOORU_VIEWER_NO_HYPR_RULES=1 booru-viewer
```

## Writing your own rules

If you're running with `BOORU_VIEWER_NO_HYPR_RULES=1` (or layering rules
on top of the defaults), here's the reference.

### Window identity

- Main window — class `booru-viewer`
- Popout — class `booru-viewer`, title `booru-viewer — Popout`

> ⚠ The popout title uses an em dash (`—`, U+2014), not a hyphen. A rule
> like `match:title = ^booru-viewer - Popout$` will silently match
> nothing. Either paste the em dash verbatim or match the tail:
> `match:title = Popout$`.

### Example rules

```ini
# Float the popout with aspect-locked resize and no animation flicker
windowrule {
    match:class = ^(booru-viewer)$
    match:title = Popout$
    float = yes
    keep_aspect_ratio = on
    no_anim = on
}

# Per-window scroll factor if your global is too aggressive
windowrule {
    match:class = ^(booru-viewer)$
    match:title = Popout$
    scroll_mouse = 0.65
}
```

### What the env vars actually disable

`BOORU_VIEWER_NO_HYPR_RULES=1` suppresses the in-code calls to:

- `dispatch resizeactive` / `moveactive` batches that restore saved
  popout geometry
- `dispatch togglefloating` on the main window at launch
- `dispatch setprop address:<addr> no_anim 1` applied during popout
  transitions
- The startup "prime" sequence that warms Hyprland's per-window
  floating cache

`BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK=1` suppresses only
`dispatch setprop address:<addr> keep_aspect_ratio 1` on the popout.
Everything else still runs.

Read-only queries (`hyprctl clients -j`, `hyprctl monitors -j`) always
run regardless — the app needs them to know where it is.

### Hyprland requirements

The `keep_aspect_ratio` windowrule and `dispatch setprop
keep_aspect_ratio` both require a recent Hyprland. On older builds the
aspect lock is silently a no-op.
