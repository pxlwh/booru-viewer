"""Tests for the Hyprland dispatch dialect split.

Hyprland 0.56 replaced the string dispatch API with a Lua one, and
`hyprctl` exits 0 on a parse error, so the old form failed silently for
0.56+ users: the popout's aspect lock, resize, move and tiling all
stopped working with nothing to notice by return code.

These cover the pure string builders in both dialects. No subprocess,
no compositor — the probe is stubbed.
"""

from __future__ import annotations

import pytest

from booru_viewer.gui.popout import hyprland as h


ADDR = "0x563e0a614090"


@pytest.fixture(autouse=True)
def _reset_cache():
    """The dialect is cached per process; clear it around every test."""
    h._lua_dialect = None
    yield
    h._lua_dialect = None


@pytest.fixture
def lua(monkeypatch):
    monkeypatch.setattr(h, "_uses_lua_dispatch", lambda: True)


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.setattr(h, "_uses_lua_dispatch", lambda: False)


# --- Lua dialect (Hyprland 0.56+) -------------------------------------

def test_lua_setprop(lua):
    got = h._setprop(ADDR, "keep_aspect_ratio", 1)
    assert got == (
        "dispatch hl.dsp.window.set_prop{ prop='keep_aspect_ratio', "
        f"value=1, window='address:{ADDR}' }}"
    )


def test_lua_resize_is_absolute_form(lua):
    """resize{x,y} is absolute, not a delta — verified live against
    0.56.2 by applying the same resize twice and seeing no change."""
    assert h._resize_exact(ADDR, 800, 600) == (
        f"dispatch hl.dsp.window.resize{{ x=800, y=600, window='address:{ADDR}' }}"
    )


def test_lua_move_is_absolute_form(lua):
    assert h._move_exact(ADDR, 100, 50) == (
        f"dispatch hl.dsp.window.move{{ x=100, y=50, window='address:{ADDR}' }}"
    )


def test_lua_set_tiled_uses_float_toggle(lua):
    """There is no force-tile in the Lua API: float TOGGLES, and an
    unrecognized state= key is ignored rather than honored. Callers
    guard on `floating` first, which is what makes this safe."""
    got = h._set_tiled(ADDR)
    assert got == f"dispatch hl.dsp.window.float{{ window='address:{ADDR}' }}"
    assert "state" not in got


@pytest.mark.parametrize("builder,args", [
    (h._setprop, (ADDR, "no_anim", 1)),
    (h._resize_exact, (ADDR, 800, 600)),
    (h._move_exact, (ADDR, 10, 20)),
    (h._set_tiled, (ADDR,)),
])
def test_lua_forms_are_balanced_and_addressed(lua, builder, args):
    got = builder(*args)
    assert got.count("{") == got.count("}"), got
    assert f"window='address:{ADDR}'" in got
    assert got.startswith("dispatch hl.dsp.window.")


# --- legacy dialect (pre-0.56) ----------------------------------------

def test_legacy_setprop(legacy):
    assert h._setprop(ADDR, "keep_aspect_ratio", 1) == \
        f"dispatch setprop address:{ADDR} keep_aspect_ratio 1"


def test_legacy_resize(legacy):
    assert h._resize_exact(ADDR, 800, 600) == \
        f"dispatch resizewindowpixel exact 800 600,address:{ADDR}"


def test_legacy_move(legacy):
    assert h._move_exact(ADDR, 100, 50) == \
        f"dispatch movewindowpixel exact 100 50,address:{ADDR}"


def test_legacy_set_tiled(legacy):
    assert h._set_tiled(ADDR) == f"dispatch settiled address:{ADDR}"


@pytest.mark.parametrize("builder,args", [
    (h._setprop, (ADDR, "no_anim", 1)),
    (h._resize_exact, (ADDR, 800, 600)),
    (h._move_exact, (ADDR, 10, 20)),
    (h._set_tiled, (ADDR,)),
])
def test_legacy_forms_carry_no_lua(legacy, builder, args):
    got = builder(*args)
    assert "hl.dsp" not in got and "{" not in got
    assert ADDR in got


# --- the probe --------------------------------------------------------

def _fake_run(stdout):
    class R:
        pass
    r = R(); r.stdout = stdout; r.stderr = ""
    return lambda *a, **k: r


def test_probe_detects_legacy_from_unknown_request(monkeypatch):
    monkeypatch.setattr(h.subprocess, "run", _fake_run("unknown request"))
    assert h._uses_lua_dispatch() is False


def test_probe_detects_lua_from_ok(monkeypatch):
    monkeypatch.setattr(h.subprocess, "run", _fake_run("ok"))
    assert h._uses_lua_dispatch() is True


def test_probe_falls_back_to_legacy_when_hyprctl_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(h.subprocess, "run", boom)
    assert h._uses_lua_dispatch() is False


def test_probe_is_cached(monkeypatch):
    calls = []
    def counting(*a, **k):
        calls.append(1)
        class R: stdout = "ok"; stderr = ""
        return R()
    monkeypatch.setattr(h.subprocess, "run", counting)
    h._uses_lua_dispatch(); h._uses_lua_dispatch(); h._uses_lua_dispatch()
    assert len(calls) == 1
