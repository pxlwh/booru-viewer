"""Pure helper that builds the kwargs dict passed to ``mpv.MPV``.

Kept free of any Qt or mpv imports so the options dict can be audited
from a CI test that only installs the stdlib.
"""

from __future__ import annotations


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

    - ``demuxer_lavf_o="protocol_whitelist=file,http,https,tls,tcp"``:
      restrict ffmpeg's lavf demuxer to HTTP(S) and local file reads.
      The default accepts ``concat:``, ``subfile:``, ``data:``,
      ``udp://``, etc. — ``concat:/etc/passwd|/dev/zero`` is the
      canonical local-file-read gadget when a hostile container is
      piped through lavf. ``file`` must stay in the whitelist because
      cached local files (``.part``, promoted cache paths) rely on it.
      ``crypto`` is intentionally omitted; it's an FFmpeg pseudo-
      protocol for AES-decrypted streams that boorus do not serve.

    - ``input_conf="/dev/null"`` (POSIX only): skip loading
      ``~/.config/mpv/input.conf``. The existing
      ``input_default_bindings=False`` + ``input_vo_keyboard=False``
      are the primary lockdown; this is defense-in-depth. Windows
      uses a different null-device path and the load behavior varies
      by mpv build, so it is skipped there.
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
        "demuxer_lavf_o": "protocol_whitelist=file,http,https,tls,tcp",
    }
    if not is_windows:
        kwargs["input_conf"] = "/dev/null"
    return kwargs
