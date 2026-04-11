"""booru_viewer.core package — pure-Python data + I/O layer (no Qt).

Side effect on import: install the project-wide PIL decompression-bomb
cap. PIL's default warns silently above ~89M pixels; we want a hard
fail above 256M pixels so DecompressionBombError can be caught and
treated as a download failure.

Setting it here (rather than as a side effect of importing
``core.cache``) means any code path that touches PIL via any
``booru_viewer.core.*`` submodule gets the cap installed first —
``core.images`` no longer depends on ``core.cache`` having been
imported in the right order. Audit finding #8.
"""

from PIL import Image as _PILImage

_PILImage.MAX_IMAGE_PIXELS = 256 * 1024 * 1024

del _PILImage
