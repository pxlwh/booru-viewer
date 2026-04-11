"""Pure helpers that build the kwargs dict passed to ``mpv.MPV`` and
the post-construction options dict applied via the property API.

Kept free of any Qt or mpv imports so the options can be audited from
a CI test that only installs the stdlib.
"""

from __future__ import annotations

# FFmpeg ``protocol_whitelist`` value applied via mpv's
# ``demuxer-lavf-o`` option (audit finding #2). ``file`` must stay so
# cached local clips and ``.part`` files keep playing; ``http``/
# ``https``/``tls``/``tcp`` are needed for fresh network video.
# ``crypto`` is intentionally omitted — it's an FFmpeg pseudo-protocol
# for AES-decrypted streams that boorus do not legitimately serve.
LAVF_PROTOCOL_WHITELIST = "file,http,https,tls,tcp"


def lavf_options() -> dict[str, str]:
    """Return the FFmpeg lavf demuxer options to apply post-construction.

    These cannot be set via ``mpv.MPV(**kwargs)`` because python-mpv's
    init path uses ``mpv_set_option_string``, which routes through
    mpv's keyvalue list parser. That parser splits on ``,`` to find
    entries, so the comma-laden ``protocol_whitelist`` value gets
    shredded into orphan tokens and mpv rejects the option with
    -7 OPT_FORMAT. mpv's documented backslash escape (``\\,``) is
    not unescaped on this code path either.

    The post-construction property API DOES accept dict values for
    keyvalue-list options via the node API, so we set them after
    ``mpv.MPV()`` returns. Caller pattern:

        m = mpv.MPV(**build_mpv_kwargs(is_windows=...))
        for k, v in lavf_options().items():
            m["demuxer-lavf-o"] = {k: v}
    """
    return {"protocol_whitelist": LAVF_PROTOCOL_WHITELIST}


def build_mpv_kwargs(is_windows: bool) -> dict[str, object]:
    """Return the kwargs dict for constructing ``mpv.MPV``.

    The playback, audio, and network options are unchanged from
    pre-audit v0.2.5. The security hardening added by SECURITY_AUDIT.md
    finding #2 is:

    - ``ytdl="no"``: refuse to delegate URL handling to yt-dlp. mpv's
      default enables a yt-dlp hook script that matches ~1500 hosts
      and shells out to ``yt-dlp`` on any URL it recognizes. A
      compromised booru returning ``file_url: "https://youtube.com/..."``
      would pull the user through whatever extractor CVE is current.

    - ``load_scripts="no"``: do not auto-load Lua scripts from
      ``~/.config/mpv/scripts``. These scripts run in mpv's context
      every time the widget is created.

    - ``input_conf="/dev/null"`` (POSIX only): skip loading
      ``~/.config/mpv/input.conf``. The existing
      ``input_default_bindings=False`` + ``input_vo_keyboard=False``
      are the primary lockdown; this is defense-in-depth. Windows
      uses a different null-device path and the load behavior varies
      by mpv build, so it is skipped there.

    The ffmpeg protocol whitelist (also part of finding #2) is NOT
    in this dict — see ``lavf_options`` for the explanation.
    """
    kwargs: dict[str, object] = {
        "vo": "libmpv",
        "hwdec": "auto",
        "keep_open": "yes",
        "ao": "pulse,wasapi,",
        "audio_client_name": "booru-viewer",
        "input_default_bindings": False,
        "input_vo_keyboard": False,
        "osc": False,
        "vd_lavc_fast": "yes",
        "vd_lavc_skiploopfilter": "nonkey",
        "cache": "yes",
        "cache_pause": "no",
        "demuxer_max_bytes": "50MiB",
        "demuxer_readahead_secs": "20",
        "network_timeout": "10",
        "ytdl": "no",
        "load_scripts": "no",
    }
    if not is_windows:
        kwargs["input_conf"] = "/dev/null"
    return kwargs
