# Hyprland integration

I daily-drive booru-viewer on Hyprland and I've baked in my own opinions
on how the app should behave there. By default, a handful of `hyprctl`
dispatches run at runtime to:

- Restore the main window's last floating mode + dimensions on launch
- Restore the popout's position and keep it anchored to its configured
  anchor point (center or any corner) as its content resizes during
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
- the `no_anim` window property applied during popout transitions
  (skipped on the first fit after open so Hyprland's `windowsIn` /
  `popin` animation can play — subsequent navigation fits still
  suppress anim to avoid resize flicker)
- The startup "prime" sequence that warms Hyprland's per-window
  floating cache

`BOORU_VIEWER_NO_POPOUT_ASPECT_LOCK=1` suppresses only the popout's
`keep_aspect_ratio` window property. Everything else still runs.

Read-only queries (`hyprctl clients -j`, `hyprctl monitors -j`) always
run regardless — the app needs them to know where it is.

### Hyprland requirements

The `keep_aspect_ratio` window property requires a reasonably recent
Hyprland. On builds too old to know the property, the aspect lock is a
no-op.

### Hyprland 0.56 and the Lua config

Hyprland 0.56 replaced the string dispatch API with a Lua one. The old
form is now parsed as Lua and fails:

```
$ hyprctl dispatch setprop address:0x1 keep_aspect_ratio 1
error: [string "return hl.dispatch(setprop address:0x1..."]:1: ')' expected near 'address'
```

`hyprctl` **exits 0 on that error**, so nothing could detect it by
return code. On 0.56 and later, every in-code dispatch this app makes
— the aspect lock, popout resize, popout move, and un-floating on
reopen — silently stopped working, which looked exactly like running
with both opt-out env vars permanently set.

Fixed in the release after v0.2.9. The app now probes once, with a read-only
`hyprctl eval`, and emits whichever dialect the running compositor
speaks. Older Hyprland keeps the legacy strings; 0.56+ gets the Lua
forms:

| what | legacy | 0.56+ |
|---|---|---|
| aspect lock | `dispatch setprop address:<a> keep_aspect_ratio 1` | `hl.dsp.window.set_prop{ prop='keep_aspect_ratio', value=1, window='address:<a>' }` |
| resize | `dispatch resizewindowpixel exact W H,address:<a>` | `hl.dsp.window.resize{ x=W, y=H, window='address:<a>' }` |
| move | `dispatch movewindowpixel exact X Y,address:<a>` | `hl.dsp.window.move{ x=X, y=Y, window='address:<a>' }` |
| un-float | `dispatch settiled address:<a>` | `hl.dsp.window.float{ window='address:<a>' }` |

Two gotchas if you are writing your own scripts against the Lua API:
`resize` and `move` take **absolute** pixels, not deltas, despite the
`relative` option existing. And `float` **toggles** — there is no
force-tile, and passing an unrecognized `state=` key is silently
ignored rather than honored, so check `floating` in `hyprctl clients -j`
before calling it.
