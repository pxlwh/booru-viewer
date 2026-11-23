from pathlib import Path
import re
import shutil
import subprocess

root = Path(__file__).resolve().parent.parent
app = root / 'build/AppDir'
bundle = app / 'usr/lib/booru-viewer'
internal = bundle / '_internal'
media = internal / 'mpv-lib'
cli = app / 'usr/bin/mpv'
share = app / 'usr/share/applications'
mpv = root / 'build/mpv-debs/usr'
libmpv = mpv / 'lib/x86_64-linux-gnu/libmpv.so.2'
mpv_cli = mpv / 'bin/mpv'

# Keep the host's libc, C++ runtime and GPU/display stack coherent.
host = re.compile(r'(?:ld-linux|lib(?:c\.so|m\.so|pthread|rt\.so|dl\.so|resolv|stdc\+\+|gcc_s|GL|EGL|OpenGL|gbm|drm|vulkan|va(?:-|\.)|vdpau|X|xcb|xkbcommon|wayland|udev|systemd))')
ldd = subprocess.check_output(['ldd', libmpv, mpv_cli], text=True)
if 'not found' in ldd:
    raise SystemExit(ldd)

shutil.copytree(root / 'dist/booru-viewer', bundle, symlinks=True)
media.mkdir(parents=True)
cli.parent.mkdir(parents=True)
share.mkdir(parents=True)
for path in internal.glob('*.so*'):
    if host.match(path.name) or path.name.startswith('libmpv.so'):
        path.unlink()

for name, path in sorted(set(re.findall(r'(?m)^\s*(\S+) => (/\S+)', ldd))):
    if not host.match(name) and not name.startswith('libmpv.so') and not (internal / name).exists():
        shutil.copy2(path, media / name)

shutil.copy2(libmpv, media / 'libmpv.so.2')
shutil.copy2(mpv_cli, cli)
for lib in media.iterdir():
    subprocess.run(['patchelf', '--force-rpath', '--set-rpath', '$ORIGIN:$ORIGIN/..', lib], check=True)
subprocess.run(['patchelf', '--force-rpath', '--set-rpath', '$ORIGIN/../lib/booru-viewer/_internal/mpv-lib:$ORIGIN/../lib/booru-viewer/_internal', cli], check=True)

shutil.copy2(root / 'packaging/AppRun', app / 'AppRun')
shutil.copy2(root / 'packaging/booru-viewer.desktop', app / 'booru-viewer.desktop')
(share / 'booru-viewer.desktop').symlink_to('../../../booru-viewer.desktop')
shutil.copy2(root / 'icon.png', app / 'booru-viewer.png')
(app / '.DirIcon').symlink_to('booru-viewer.png')
(cli.parent / 'booru-viewer').symlink_to('../../AppRun')

licenses = app / 'usr/share/doc/booru-viewer'
licenses.mkdir(parents=True)
shutil.copy2(root / 'LICENSE', licenses)
shutil.copy2(root / 'licenses/THIRD-PARTY-NOTICES.txt', licenses)
for package in ('mpv', 'libmpv2', 'libavcodec60'):
    doc = Path('/usr/share/doc') if package == 'libavcodec60' else mpv / 'share/doc'
    shutil.copy2(doc / package / 'copyright', licenses / f'{package}.copyright')
for name in ('GPL-2', 'GPL-3', 'LGPL-2.1'):
    shutil.copy2(Path('/usr/share/common-licenses') / name, licenses)
