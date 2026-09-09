# Trusted scientific adapters (unified visualization contract)

These Vue components are build-time trusted implementations, not scripts chosen
by a manifest. Every component receives `{ file: FileInfo, plugin:
VisualizationPlugin }` and uses the unified `visualizations/runtime.ts`
authorization, operation, version and bounded-byte boundary. The local
`extended/runtime.ts` helper only adapts unified typed payloads to SDK-facing
data; it does not define another public protocol or endpoint. All resources belong to `usePreviewLoad` and
are released when another file/view is selected or the plugin is stopped.

Plotly and JSROOT load their package-owned, version-checked browser distributions
from two fixed same-origin asset paths. This avoids parsing their prebuilt
distributions again and prevents JSROOT's Node-only `resvg` export branch from
entering the application bundle. No file or manifest can select a script URL;
cancelled viewers never mount a late-loaded library. vtk.js XMLReader uses a
scoped native DOMParser bridge instead of the Node-oriented xmlbuilder2 package;
the bridge rejects appended/binary XML and is not a global Node polyfill.
Plotly uses the official `plotly.js-cartesian-dist-min` 4.1.0 distribution,
covering the supported 2D traces without the full bundle's unrelated map engine
and eagerly allocated map-worker Blob URL.

NMRium is separately prebuilt by `visualization-prebuild.ts` in the asset
plugin's `buildStart` hook for both development and production. The fixed ESM
bundle owns its React renderer and exposes only `mountNmr(container,
NumericNmrSpectrum)` plus disposal; application components cannot provide
workspaces or import sources. Its JavaScript, stylesheet and fonts use the same
local `/visualization-assets/nmrium/` prefix in both modes. This is a real build
step, not a manually generated file that CI might miss. Legal comments remain in
the generated linked notice file. Splitting this workbench reduced the full
application Rollup graph from approximately 13,965 modules to 8,602 and allowed
the default Node heap build to complete without increasing its heap allowance.

| Adapter | Real upstream API | Supported first integration |
| --- | --- | --- |
| `plotly` | Plotly `react` / `purge` | CSV/TSV bounded windows; NPY/NPZ/MAT numeric variables and slices; curves, scatter, histogram, array heatmap |
| `h5web` | H5Web `LineVis` / `HeatmapVis`, React root | Controlled HDF5/NeXus numeric tree, leading-dimension slice selection; no remote H5Grove/HSDS service |
| `vtk` | vtk.js readers, mapper, actor, GenericRenderWindow | VTP/VTI **uncompressed ASCII numeric XML**, STL, OBJ without external materials; VTI axial slice, not full-volume rendering |
| `jsroot` | JSROOT `createHistogram`, `createTGraph`, `draw`, `cleanup` | Server-extracted TH1/TH2/TGraph numeric values; preserve real bin edges; no file-provided ROOT object reaches `draw` |
| `molstar` | Mol* core `PluginContext`, rawData, parseTrajectory | Small PDB/mmCIF atom structures; no upstream download actions or property-fetch behaviors |
| `nmrium` | NMRium React component | Processed one-dimensional JCAMP-DX numeric spectra with explicit ppm and nucleus; no FID processing or external imports |

All 34 plugin registrations use strict public contract version 2; these adapters
do not form a separate extended protocol. Approved reader/view/capability
combinations come from `contracts/visualization-adapters.json` and its generated
build copies. New adapters must update that source, run
`node scripts/sync-visualization-contract.mjs` and `--check`, implement the
trusted renderer/reader, and add bounded-input, cancellation and SDK tests.
Use `preview` for structured readers and `bytes` for bounded local decoding;
private worker formats are normalized by the host to one typed result envelope.

## Reading and limits

The root/jcamp/hdf5/tabular readers run in the existing restricted worker.
`data.ts` rejects non-finite values, excessive arrays, malformed dimensions and
untrusted object cells before handing values to upstream packages. Missing array
values remain NaN/gaps for H5Web/Plotly. ROOT numeric reconstruction rejects
missing values instead of silently replacing them with zero.
Table booleans remain typed values; Plotly explicitly maps `false` to `0` and
`true` to `1` for numerical axes, with this mapping shown in the preview.

VTK is bounded to 8 MiB input, 100,000 STL triangles, 300,000 points, 2,097,152
VTI voxels and a conservative 32 MiB declared numeric-array budget. Compressed,
appended binary, external entities, StringArray-style arrays and external OBJ
materials are rejected. This does **not** imply support for all VTK XML files.
Mol* uses at most 8 MiB UTF-8 input and at most 100,000 PDB atom records. It does
not load Mol* state files, trajectories, density maps or remote identifiers.
It selects the model structure rather than expanding file-declared biological
assemblies that could multiply the accepted atom budget.

H5Web uses the actual original index spacing from the reader when data are
strided. ROOT uses the supplied x/y bin edges, including nonuniform bins. Error
graph inputs may show coordinates without their errors; the reader's warning is
shown rather than claiming error-bar support.

## NMRium license and isolation choice

NMRium is pinned to **0.60.0** (MIT). The installed `nmr-load-save` 0.37.x and
`nmr-processing` 12.x line are MIT. Newer NMRium distributions depend on cores or
processing libraries licensed CC-BY-NC-SA-4.0; do not upgrade this dependency by
looking only at the top-level `nmrium` license. Review the complete lockfile.

The adapter does not use the permissive upstream `embedded` workspace: a
dedicated workspace disables all imports, editing/processing tools, databases,
external APIs and settings. It constructs a fresh numeric spectrum and never
passes file `source`, URLs, filters or saved workspaces to NMRium. Drop and paste
events are blocked; only zoom is enabled. It does not persist original files.
The fixed bundle maps NMRium's own LocalStorage utility to a scoped, in-memory
bridge, preventing historical workspace restoration and browser-storage writes.
This does not replace or change the host application's storage APIs.

## Verification

`node --test tests/extendedScientificVisualizations.test.mjs` checks actual
JSROOT object construction, bin orientation, missing values, parser allocation
budgets, Vue cancellation/late-response behavior, and the NMRium data boundary.
`npm run type-check` checks the installed upstream API declarations. GPU visual
checks and the full production build are separate integration gates; unit tests
alone are not proof of visual rendering in every browser.

The shared `tests/browser/domains-smoke.mjs` harness also accepts
`VISUALIZATION_BROWSER_CASES=plotly,jsroot,h5web,vtk,molstar,nmrium` and loads the
six real SDKs with synthetic byte buffers/numeric worker responses. It does not
open a server or touch production routes/data. All network requests are fulfilled
from local assets or rejected; zero external attempts, console errors, and leaked
workers/blob URLs are required. Assertions check actual SVG paths or WebGL draw
calls, usable H5Web/NMR plot height, and teardown. The NMR case injects an untrusted
historical workspace before mounting and verifies that it is never read.
