# Third-Party Notices

`hdrgainmap` itself is released under the MIT License (see `LICENSE`). It uses
the components below. None of their binaries are redistributed in this
repository: Python packages are installed from PyPI, and FFmpeg is installed
separately by the user.

## Python dependencies

| Component | Version used | License | License / source |
| --- | --- | --- | --- |
| NumPy | 2.5.5 | BSD-3-Clause | <https://numpy.org/doc/stable/license.html> |
| Pillow | 13.0.0 | MIT-CMU | <https://python-pillow.org/> · <https://raw.githubusercontent.com/python-pillow/Pillow/main/LICENSE> |
| pillow-heif | 1.9.0 | BSD-3-Clause | <https://github.com/bigcat88/pillow_heif> · bundled notices: <https://github.com/bigcat88/pillow_heif/blob/master/LICENSES_bundled.txt> |

`pillow-heif` wheels bundle `libheif` and codec libraries. In the wheel used
here that means `libheif` 1.23.7 (LGPL-3.0), the `x265` HEVC encoder
(GPL-2.0-or-commercial) and the `libde265` HEVC decoder (LGPL-3.0). Their
license texts ship in the wheel's `LICENSES_bundled.txt`.

## External tools invoked at runtime

These are separate executables called through `subprocess`; this project does
not link against their libraries.

| Component | Used for | License | License / source |
| --- | --- | --- | --- |
| FFmpeg 9.1 (gyan.dev full build) | AVIF encode/decode, pipe fallback | FFmpeg: LGPL-2.1-or-later. The local build is configured with `--enable-gpl --enable-version3 --enable-libx264 --enable-libx265`, so the binary as a whole is GPLv3. | <https://ffmpeg.org/legal.html> |
| libaom | AV1 encoding (through FFmpeg) | BSD-style license | <https://aomedia.googlesource.com/aom/+/refs/heads/main/LICENSE> |
| SVT-AV1 | AV1 encoding fallback (through FFmpeg) | BSD-3-Clause Clear | <https://gitlab.com/AOMediaCodec/SVT-AV1/-/blob/master/LICENSE.md> |
| x264 | H.264 encoding (through FFmpeg) | GPL-2.0 | <https://www.videolan.org/developers/x264.html> |
| x265 | HEVC encoding (through FFmpeg and pillow-heif wheels) | GPL-2.0 or commercial | <https://www.x265.org/> · <https://bitbucket.org/multicoreware/x265_git/raw/master/COPYING> |
| libheif | HEIC container handling (through pillow-heif) | LGPL-3.0 (library); MIT for sample apps/wrappers | <https://github.com/strukturag/libheif/blob/master/COPYING> |
| libde265 | HEVC decoding (through libheif) | LGPL-3.0 | <https://github.com/strukturag/libde265/blob/master/COPYING> |

## Compliance notes

- This repository distributes only original Python source code under MIT. It
  does not include FFmpeg, libheif, x264, x265, libaom or SVT-AV1 binaries.
- FFmpeg is executed as a separate program. Because there is no linking, the
  MIT license of this project is not changed by the GPLv3 FFmpeg build.
- If you redistribute the GPLv3 FFmpeg build or a `pillow-heif` wheel that
  bundles `x265`, you must comply with the GPLv3 / LGPL-3.0 terms: ship the
  license texts, provide the corresponding source or a written offer, and keep
  the notices intact.
- No additional license purchase is required for the dependencies as used
  here. The commercial x265 / libheif options matter only if you redistribute
  binaries and cannot accept GPL / LGPL terms.
