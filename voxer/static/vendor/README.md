# Vendored overlay rendering dependencies

The browser source serves executable JavaScript locally. It never downloads code
from a CDN at runtime. These files retain the versions previously used by the
project; they are pinned assets, not a new npm build/runtime dependency.

- Three.js 0.160.0: https://registry.npmjs.org/three/-/three-0.160.0.tgz
- PixiJS 7.4.2: https://registry.npmjs.org/pixi.js/-/pixi.js-7.4.2.tgz
- PixiJS CSP adapter 7.4.2: https://registry.npmjs.org/@pixi/unsafe-eval/-/unsafe-eval-7.4.2.tgz
- Retrieved from the public npm registry on 2026-09-05.
- Upstream licenses: `three/LICENSE`, `PIXI-LICENSE` and
  `PIXI-UNSAFE-EVAL-LICENSE` (MIT).

Three.js `build/three.module.js`, `examples/jsm/loaders/GLTFLoader.js`, and
`examples/jsm/utils/BufferGeometryUtils.js` were copied from the package. The
loader and utilities import paths were rewritten to adjacent local modules.
PixiJS `dist/pixi.min.js` is copied unchanged. Source maps are not shipped.
The adapter's `dist/unsafe-eval.min.js` is copied unchanged as
`pixi-unsafe-eval.min.js` (SHA-256:
`8eafd460d82359fc9defc3c9f2f8733cf4bb18a40cba632c751b162745fb9f80`).
Despite its package name, this adapter **removes** Pixi's need for dynamic
`new Function` calls. Load it after Pixi and before creating the renderer; keep
`script-src 'self'` without `unsafe-eval` or `unsafe-inline`.

When updating, review upstream release/security notes, replace the package files
and licenses, preserve the local import graph, and exercise the full OBS overlay
on the target browser engine. Do not restore executable CDN imports.
