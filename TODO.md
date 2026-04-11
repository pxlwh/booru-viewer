# booru-viewer follow-ups

Items deferred from the 2026-04-10 security audit remediation that
weren't safe or in-scope to fix in the same branch.

## Dependencies / supply chain

- **Lock file** (audit #9): runtime deps now have upper bounds in
  `pyproject.toml`, but there is still no lock file pinning exact
  versions + hashes. Generating one needs `pip-tools` (or `uv`) as a
  new dev dependency, which was out of scope for the security branch.
  Next pass: add `pip-tools` to a `[project.optional-dependencies] dev`
  extra and commit a `requirements.lock` produced by
  `pip-compile --generate-hashes`. Hook into CI as a `pip-audit` job.

## Code quality

- **Dead code in `core/images.py`** (audit #15): `make_thumbnail` and
  `image_dimensions` are unreferenced. The library's actual
  thumbnailing happens in `gui/library.py:312-321` (PIL inline) and
  `gui/library.py:323-338` (ffmpeg subprocess). Delete the two unused
  functions next time the file is touched. Out of scope here under
  the "no refactors" constraint.
