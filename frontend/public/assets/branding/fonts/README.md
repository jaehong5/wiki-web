# Bundled Wiki fonts

These font files are served from this site; no third-party font request is needed.
The complete original character sets are retained, including all 11,172 modern
Hangul syllables in each face. No text-derived or page-specific subset is used.

## Wanted Sans

- Project: https://github.com/wanteddev/wanted-sans
- Release: [v1.0.3](https://github.com/wanteddev/wanted-sans/releases/tag/v1.0.3)
- Pinned commit: `f4f2f8fd93c19f1399cf5a7d17d05bb4b103ac14`
- File: `wanted-sans-variable-1.0.3.woff2`
- Source: `packages/wanted-sans/fonts/webfonts/variable/complete/woff2/WantedSansVariable.woff2`
- Original WOFF2 copied without modification.
- Internal family: Wanted Sans Variable; version 1.003.
- Variable weight axis: 400–1000, default 400; normal style.
- Copyright: The Wanted Sans Project Authors.
- License: SIL Open Font License 1.1, included in `OFL-Wanted-Sans.txt`.

## D2Coding

- Project: https://github.com/naver/d2-coding-font
- Release: [VER1.4.0](https://github.com/naver/d2-coding-font/releases/tag/VER1.4.0), 2026-10-03.
- Pinned commit: `b0dc372e28d7abdba7f3cba854855ceffbd490a8`
- Files: `d2coding-regular-1.4.0.woff2` (400), `d2coding-bold-1.4.0.woff2` (700).
- Sources: `fonts/ttf/D2Coding-Regular.ttf`, `fonts/ttf/D2Coding-Bold.ttf`.
- Standard faces without programming ligatures. The upstream specimen WOFF2 files
  were not used because they contain the separate ligature faces.
- Internal family: D2Coding; version 1.4.0, build 20261003; normal style.
- Copyright: NAVER Corporation; reserved font names D2Coding and D2Coding-Bold.
- License: SIL Open Font License 1.1, included in `OFL-D2Coding.txt`.

The standard upstream TTFs were compressed with `fontTools.ttLib.woff2.compress`
(fontTools 4.66.1 and Brotli 1.2.0). The conversion preserves the character map,
advances, outlines, hint programs, names, copyright and license metadata. No
subsetting, glyph edits, metadata edits, or optional WOFF-specific metadata were
introduced. WOFF2 changes only the compression/representation of the font data.
This follows the [OFL WOFF/WOFF2 guidance](https://openfontlicense.org/ofl-faq/#2-2-can-i-make-and-use-woff-web-open-font-format-versions-of-ofl-fonts).

```python
from fontTools.ttLib.woff2 import compress
compress("D2Coding-Regular.ttf", "d2coding-regular-1.4.0.woff2")
compress("D2Coding-Bold.ttf", "d2coding-bold-1.4.0.woff2")
```

## Verification

`sources.json` records the original pinned URLs, SHA-256 checksums, byte sizes,
conversion tool versions and parsed metadata. The three webfonts contain full
Hangul coverage; D2Coding also retains its original Hanja and symbol repertoire.
The site stylesheet declares these faces with `font-display: swap`. Only Wanted
Sans is preloaded; D2Coding downloads when code or a source document is displayed.
The existing W logo is independent of these fonts.
