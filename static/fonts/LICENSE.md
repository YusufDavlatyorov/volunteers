# Self-hosted fonts

Loaded by `static/css/fonts.css` (a verbatim copy of the Google Fonts CSS with
local URLs). Both families are licensed under the SIL Open Font License 1.1,
which permits bundling and redistribution: https://openfontlicense.org

| Family | Copyright | Source |
|---|---|---|
| Inter (variable, 400–800) | Copyright 2020 The Inter Project Authors | https://github.com/rsms/inter |
| Playfair Display (variable 600–800, italic 600) | Copyright 2017 The Playfair Display Project Authors | https://github.com/clauseggers/Playfair-Display |

Files are the per-script subsets Google Fonts serves (`*-latin`, `*-cyrillic`,
`*-cyrillic-ext`, …); `unicode-range` in `fonts.css` makes a browser fetch only
the subsets a page uses. To refresh them, re-download the CSS from
`https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Playfair+Display:ital,wght@0,600;0,700;0,800;1,600&display=swap`
with a current Chrome user agent and repeat the URL rewrite.
