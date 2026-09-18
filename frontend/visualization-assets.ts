/** Package-owned offline assets only. Manifests cannot select filesystem paths. */
import { cp, mkdir, stat } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { resolve, sep } from 'node:path';
import type { Plugin } from 'vite';
import { nmrBrowserDirectory, prebuildNmrBrowser, docxBrowserDirectory, prebuildDocxBrowser } from './visualization-prebuild';

export function visualizationAssets(): Plugin {
  const roots: Record<string, string> = {
    '/visualization-assets/domains/': resolve('src/visualizations/extended/domains/assets'),
    '/visualization-assets/cesium/': resolve('node_modules/cesium/Build/Cesium'),
    '/visualization-assets/maplibre/': resolve('node_modules/maplibre-gl/dist'),
    '/visualization-assets/pdfjs/cmaps/': resolve('node_modules/pdfjs-dist/cmaps'),
    '/visualization-assets/pdfjs/standard_fonts/': resolve('node_modules/pdfjs-dist/standard_fonts'),
    '/visualization-assets/pdfjs/wasm/': resolve('node_modules/pdfjs-dist/wasm'),
    '/visualization-assets/aladin/': resolve('node_modules/aladin-lite/dist'),
    '/visualization-assets/jsroot/': resolve('node_modules/jsroot/build'),
    '/visualization-assets/plotly/': resolve('node_modules/plotly.js-cartesian-dist-min'),
    '/visualization-assets/nmrium/': nmrBrowserDirectory(),
    '/visualization-assets/docx/': docxBrowserDirectory(),
  };
  let output = resolve('dist');
  return {
    name: 'dataseek-offline-visualization-assets',
    async buildStart() { await Promise.all([prebuildNmrBrowser(), prebuildDocxBrowser()]); },
    configResolved(config) { output = resolve(config.root, config.build.outDir); },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        try {
          const path = decodeURIComponent(new URL(req.url ?? '/', 'http://localhost').pathname);
          const prefix = Object.keys(roots).find(key => path.startsWith(key));
          if (!prefix) return next();
          const source = resolve(roots[prefix], path.slice(prefix.length));
          if (!source.startsWith(roots[prefix] + sep) || !(await stat(source)).isFile()) { res.statusCode = 404; res.end(); return; }
          const extension = source.split('.').pop();
          const types: Record<string, string> = { js: 'application/javascript', mjs: 'application/javascript', css: 'text/css', wasm: 'application/wasm', json: 'application/json', xml: 'application/xml', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', svg: 'image/svg+xml' };
          res.setHeader('Content-Type', types[extension ?? ''] ?? 'application/octet-stream');
          res.setHeader('X-Content-Type-Options', 'nosniff');
          createReadStream(source).on('error', () => { res.statusCode = 404; res.end(); }).pipe(res);
        } catch { res.statusCode = 404; res.end(); }
      });
    },
    async closeBundle() {
      for (const [prefix, source] of Object.entries(roots)) {
        const target = resolve(output, prefix.slice(1));
        await mkdir(target, { recursive: true });
        if (prefix.includes('cesium')) {
          for (const directory of ['Assets', 'Widgets', 'Workers', 'ThirdParty']) await cp(resolve(source, directory), resolve(target, directory), { recursive: true });
          await cp(resolve(source, 'Cesium.js'), resolve(target, 'Cesium.js'));
          await cp(resolve(source, '../../LICENSE.md'), resolve(target, 'LICENSE.md'));
        } else if (prefix.includes('maplibre')) {
          for (const file of ['maplibre-gl.mjs', 'maplibre-gl.css', 'maplibre-gl-worker.mjs', 'maplibre-gl-shared.mjs']) await cp(resolve(source, file), resolve(target, file));
          await cp(resolve(source, '..', 'LICENSE.txt'), resolve(target, 'LICENSE.txt'));
        } else if (prefix.includes('aladin')) {
          await cp(resolve(source, 'aladin.js'), resolve(target, 'aladin.js'));
        } else if (prefix.includes('jsroot')) {
          await cp(resolve(source, 'jsroot.min.js'), resolve(target, 'jsroot.min.js'));
          await cp(resolve(source, '..', 'LICENSE'), resolve(target, 'LICENSE'));
        } else if (prefix.includes('plotly')) {
          await cp(resolve(source, 'plotly-cartesian.min.js'), resolve(target, 'plotly-cartesian.min.js'));
          await cp(resolve(source, 'LICENSE'), resolve(target, 'LICENSE'));
        } else {
          await cp(source, target, { recursive: true });
          if (prefix.includes('/docx/')) {
            await cp(resolve('node_modules/docx-preview/LICENSE'), resolve(target, 'docx-preview-LICENSE'));
            await cp(resolve('node_modules/jszip/LICENSE.markdown'), resolve(target, 'JSZip-LICENSE.markdown'));
          }
        }
      }
    },
  };
}
