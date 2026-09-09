# Cordis scientific domain adapters (unified protocol)

These trusted, lazy Vue adapters receive only `{ file, plugin }`. Each invocation
uses `loadPluginBytes()` through the unified owner- and plugin-gated `bytes`
operation at `/files/{id}/visualization`; no renderer
receives a host path, arbitrary user URL, credential, or JavaScript entrypoint.
The descriptor's input limit is additionally bounded by the host runtime to
64 MiB for these domain plugins. They do not add Agent tools, sessions, model
calls, jobs, or SSE events. All 34 visualizations use the same public descriptor
and capability contract (currently strict version 2), independent of worker
implementation details. The approved map lives in
`contracts/visualization-adapters.json`; changes require regenerating its three
build copies and passing `node scripts/sync-visualization-contract.mjs --check`.
Input budgets, raw decoding checks and existing plugin IDs remain unchanged.

| Component | Actual upstream engine | Implemented file subset and display |
| --- | --- | --- |
| `OpenLayersPreview.vue` | OpenLayers 10.10 / geotiff.js 3.0 | RFC 7946 GeoJSON; axis-aligned GeoTIFF with explicit EPSG:4326 or EPSG:3857, first-band linear color ramp, max 1024×1024 output |
| `DeckMapPreview.vue` | MapLibre 6.8 / deck.gl 9.3 | Local GeoJSON points, lines and polygons; empty offline background; GPU overlay synchronized to map camera |
| `CesiumPreview.vue` | CesiumJS 1.145 | Local GeoJSON or CZML 1.0 numeric `cartographicDegrees` positions/trajectories; WGS84 ellipsoid, no ion/terrain/3D Tiles/network models |
| `AladinPreview.vue` | Aladin Lite 3.8.2 | Uncompressed primary-HDU 2-D FITS with RA/DEC WCS; sky coordinate grid, no online HiPS/catalog |
| `IgvPreview.vue` | IGV.js 3.8.7 | Local BED annotation or VCF core alleles; user must provide actual reference-assembly `chrom.sizes`; no automatic genome/sequence fetch |
| `VivPreview.vue` | Viv loaders/layers 0.22.1 | Single-file OME-TIFF, up to 6 noninterleaved scalar channels and 1024 Z/T/channel planes; channel switches and explicit Z/T slice controls |
| `NiivuePreview.vue` | NiiVue 0.69 | Uncompressed single-file NIfTI-1 `.nii` or embedded `raw` `.nrrd`; orthogonal slices and volume rendering for research, not diagnosis |

Exact dependency pins are in the root frontend lockfile. Viv requires a coherent
deck.gl/luma.gl 9.3 peer set. Viv currently includes geotiff 2 internally: do not
pass a geotiff 3 `Pool` or source object into its reader. It receives a local
`File`, runs without a decoder worker pool, and supplies bounded decoded planes
to `ImageLayer`. Source arrays are not sent to model context.

## Bounds and unsupported cases

- GeoJSON: 10,000 features / 100,000 finite geographic coordinates. Arbitrary
  properties, HTML, resource URLs and styles are not forwarded. Explicit non-WGS84
  CRS and GeometryCollection are rejected, not silently reprojected.
- CZML: 1,000 entities / 100,000 position samples, ascending timestamps, finite
  geographic bounds. URI/image/reference/model fields are rejected. Display
  point/path styles are reconstructed locally; arbitrary CZML is not evaluated.
- Raster/volume: at most 16,777,216 pixels/voxels and a conservative 128 MiB
  decoded array budget. GeoTIFF first band is sampled; OME planes are decoded
  sequentially with aggregate channel allocation checked. Synchronous codecs
  are bounded but cannot be advertised as preemptively interruptible.
- FITS: missing celestial WCS should use the existing numeric FITS preview;
  extension HDUs, image cubes and compressed FITS are not part of this adapter.
- IGV: up to 50,000 records. The supplied chromosome lengths are checked against
  every record, never guessed from a maximum observed coordinate. VCF display
  keeps core position/alleles/quality/filter, but omits arbitrary INFO, genotype,
  external links and breakend fields. No claim of reference-bases validation is
  made. BAM/CRAM, bigWig and indexed companion resources need the future
  authorized multi-file/range protocol, not direct download URLs.
- Viv: no companion OME XML, multifile references, XML entities, interleaved RGB,
  directory Zarr, or remote chunks. Current colors and per-plane min/max ramps
  are display choices, not modified source measurements.
- NiiVue: reject compressed and paired-volume files, NIfTI datatype/bitpix
  mismatches, detached NRRD data, byte skips and compressed encodings before
  handing bytes to the engine. DICOM and diagnostic claims are out of scope.

## Offline assets and lifecycle

The build serves Cesium assets at `/visualization-assets/cesium/` and the pinned
self-contained Aladin module at `/visualization-assets/aladin/aladin.js`.
MapLibre's module, CSS, worker and shared module live together under
`/visualization-assets/maplibre/`. IGV explicitly imports `igv/dist/igv.esm.js`:
the package's `browser` field points at an IIFE without the required ESM export.
No CDN import is used. Aladin 3.8 uses `A.image(url, options)`, not the newer
`A.imageFITS` alias. Its `survey: []` disables the default survey; `null` would
fall back to a remote default. Its dedicated frame CSP blocks remote catalog,
survey and image requests; removing the frame tears down its RAF/WASM realm.
The Aladin logo/license attribution remains present.

Cesium's geometry TaskProcessor pools and MapLibre's global RTL dispatcher can
outlive individual viewers. They therefore run in dedicated same-origin frames,
not a shared parent realm; unmount destroys the complete worker-owning realm.
The fixed empty `/visualization-assets/domains/host.html` gives those frames a
real HTTP URL. `about:srcdoc` is unsuitable for Cesium: its origin comparison
misclassifies relative worker IDs and leaves geometry initialization pending.
MapLibre's camera is mirrored into the disposable parent deck.gl overlay through
source/origin/nonce-checked messages. The listener is removed before frame
destruction, and late messages cannot update a disabled view. No dataset text is
interpolated into iframe HTML or scripts: only validated structured-clone data
crosses the boundary. Both frames block remote network origins. Cesium alone
allows script evaluation and its own embedded Blob scripts inside its isolated
frame because its pinned upstream Knockout/Emscripten bindings construct
functions and its embedded worker loader calls `importScripts(blob:...)`.
No returned dataset code is evaluated and the parent application's CSP is
unchanged. The GeoJSON smoke case waits for complete geometry loading and camera
placement, not merely a canvas or point marker; the verified image contains the
full line plus ellipsoid surface. Initial scene updates complete the geometry
work, then the viewer returns to on-demand rendering.

All adapters use a scope with an AbortSignal, deterministic disposal and
late-completion guards. MapLibre/OpenLayers/Cesium/deck/NiiVue destroy their
viewers, ResizeObservers disconnect, local Blob URLs revoke, and viewer buffers
are released. Plugin disable/unmount therefore stops the adapter and prevents
late reads from attaching a stale view. Heavy raw codecs remain browser-bound
for the explicitly small input subset; large datasets need isolated worker/
range extensions rather than relaxed limits.

## Verification

`node --test tests/visualizationExtendedDomains.test.mjs` covers 53 executable
tests: positive fixtures, invalid coordinates/references, giant allocations,
NIfTI/NRRD/FITS headers, faithful BED/VCF coordinates and filters, Vue compilation,
authorized transport, SDK entry compatibility and isolated lifetime boundaries.

`node tests/browser/domains-smoke.mjs` provides real Chrome SDK acceptance with
small in-memory fixtures, screenshots, console/network capture and disposal
checks. Every request is intercepted: this opens no listening port and does not
read production datasets or call production APIs. Nine domain scenarios cover
GeoJSON/GeoTIFF, map-camera synchronization, GeoJSON/CZML globes, FITS/WCS, explicit
BED reference tracks, two-channel OME-TIFF and a NIfTI volume. The same runner
includes scientific and office fixtures when present. GPU tests require real
draw calls; IGV checks painted annotation pixels; teardown checks stopped draws,
no active workers/Blob URLs and removal of child frames. Synthetic fixtures
verify SDK integration, not accuracy of every possible scientific file.

Requirements: installed frontend dependencies, local bundled visualization assets
and project CSS (normally from `npm run build`), Playwright and a compatible Chrome
or Chromium. The runner resolves a project-installed Playwright first, then the
Codex bundled runtime if available. Override with `VISUALIZATION_PLAYWRIGHT_MODULE`
(absolute module path) and `VISUALIZATION_BROWSER_EXECUTABLE` (browser executable).
`VISUALIZATION_BROWSER_CASES=maplibre-deck,cesium-czml` selects a small subset.
Output paths are printed as `BROWSER_REPORT=...`; artifacts live in a temporary
directory. On a host with restricted browser sandbox access, the test may require
the normal local execution approval. Deployment smoke tests remain separate.
