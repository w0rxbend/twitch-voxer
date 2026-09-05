# Skill-guided improvement review — 2026-09-05

This follow-up builds on the existing, uncommitted review work. It fixes a
repeated-cancellation persistence race, inconsistent audio filename validation,
a broken Pixi renderer under CSP, and overlay accessibility/lifecycle gaps.
The Starlette/plain-JavaScript stack and existing JSON formats are retained.

## Security findings addressed

### 1. Medium — Inline scripts remained permitted

- Location: `voxer/server.py:38`, both overlay HTML entrypoints.
- Rule: JS-CSP-002.
- Evidence: the previous policy contained `script-src 'self' 'unsafe-inline'`,
  and both pages contained inline JavaScript and an `onerror` attribute.
- Impact: CSP would not stop injected inline JavaScript if an HTML injection
  were introduced. No exploitable HTML injection was demonstrated in this pass.
- Fix: move adapters to `static/full.js` and `static/simple.js`, remove inline
  event attributes, and enforce `script-src 'self'`. DOM updates continue to use
  text content and validated image URLs. Inline styles remain permitted for
  the existing CSS and calculated particle positions.
- Verification: HTML parsing regressions require local external scripts and
  reject inline event attributes; Chromium exercises actual playback under CSP.

### 2. Low — Audio filename rules disagreed across boundaries

- Location: `voxer/models.py:39`, `voxer/server.py:164`,
  `voxer/static/overlay.js:82`.
- Rule: FASTAPI-FILES-001's Starlette file-serving guidance.
- Evidence: the producer accepted dotted names that the server rejected; the
  browser's `$` regex anchor also accepted a final newline.
- Impact: a caller could create an undeliverable audio URL or an acknowledgement
  that never matched its receipt. This was not a demonstrated traversal exploit.
- Fix: one pure Python filename predicate for creation and delivery, with the
  same strict basename contract checked at the browser boundary. Symlink,
  traversal, authentication and receipt-ownership checks remain in place.
- Verification: producer-to-HTTP contract tests and Node rejection tests cover
  dotted, oversized, leading underscore/hyphen and trailing-newline names.

## Application of all nine requested skills

| Skill | Concrete result |
| --- | --- |
| async-python-patterns | Keep the store lock until its writer exits even after repeated cancellation; preserve cancellation and retrieve writer failures. |
| python-performance-optimization | Profile emoji extraction, replace its duplicate tokenization with one token stream, and compare output and timings. |
| python-testing-patterns | Add deterministic thread/event coordination for the persistence race, filename boundary tests and complex Unicode regressions. |
| python-design-patterns | Keep file I/O in stores, filename values in models, and rendering in separate visual adapters. |
| architecture-patterns | Share the producer/delivery domain contract and retain `app.py` as the composition root without adding framework or repository layers. |
| modern-javascript-patterns | Use block-scoped state and a single image-URL normalization pass; restore cached pages with fresh socket ownership. |
| security-best-practices | Tighten script CSP and validate executable assets and audio paths. Use the applicable Starlette portions of the FastAPI reference; no FastAPI migration. |
| web-design-guidelines | Add language/title metadata, semantic focused autoplay buttons, live status, image dimensions, bounded narrow layouts and reduced-motion behavior. |
| webapp-testing | Add `tests/browser_overlay.py`: isolated AudioServer, generated MP3, real Chromium/WS playback and screenshots; no Twitch grants or model loading. |

## Rendering defect and accessibility findings

- `voxer/static/index.html:354` — Pixi previously failed initialization because
  it requires dynamic shader functions by default. The matching local
  `@pixi/unsafe-eval` 7.4.2 adapter supplies CSP-compatible uniform handling.
  The policy still forbids `unsafe-eval`; the package license and digest are
  recorded in `voxer/static/vendor/README.md`.
- `voxer/static/overlay.css:18` — both overlays honor reduced motion; JavaScript
  also stops decorative work, including changes during playback.
- `voxer/static/overlay.css:5` — long usernames and eight emotes fit within a
  360px viewport without overflowing the card.
- `voxer/static/overlay.js:144` — a focused native Enable Audio button supports
  click, Enter and Space. Tab navigation no longer starts playback.
- `voxer/static/overlay.js:275` — returning from the back-forward cache opens a
  fresh socket and discards playback ownership from the old connection.

## Performance evidence

The baseline cProfile run spent 0.549s of 1.252s in the second emoji-removal
pass across 1,000 emoji-heavy messages. The final implementation uses
`emoji.analyze` once, retaining separate images for nonstandard joined sequences.
Differential checks matched all 5,225 entries in the installed emoji data, plus
standalone variation selectors and joiners.

Best of five local timeit runs, 1,000 calls per run:

| Input | Before | After | Speed ratio |
| --- | ---: | ---: | ---: |
| 32-character plain chat | 0.0316s | 0.0200s | 1.58× |
| 380-character emoji-heavy chat | 0.4573s | 0.3243s | 1.41× |

These are function microbenchmarks on this machine, not native inference or
end-to-end latency measurements.

## Validation and limits

- Locked development environment sync passed.
- Ruff lint/format and Pyright passed.
- 381 Python tests and 9 Node tests passed.
- Four Chromium scenarios passed: both overlays with normal and reduced motion,
  keyboard autoplay recovery, narrow layout, playback completion, receipt
  deletion, local resources and browser console/page errors.
- Wheel packaging includes the new scripts, stylesheet, Pixi adapter and license.

Real Twitch authorization, chat, OBS, native synthesis quality, hardware GPU load
and deployed TLS proxies were not exercised. This is a targeted follow-up, not
an exhaustive authentication, dependency or deployment audit. Filesystem writes
and native inference still cannot be forcibly interrupted inside Python threads.

References: [Vercel Web Interface Guidelines](https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md),
[Pixi CSP adapter](https://raw.githubusercontent.com/pixijs/pixijs/v7.4.2/packages/unsafe-eval/src/install.ts),
[reduced motion](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/@media/prefers-reduced-motion).
