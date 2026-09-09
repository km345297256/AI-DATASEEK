import { resolve } from 'node:path';
import type { Plugin } from 'vite';

/** Only this audited vtk.js reader requires xmlbuilder's tiny DOM wrapper.
 * Do not add global Node polyfills or make server-only code browser-callable. */
export function visualizationBrowserBoundary(): Plugin {
  return {
    name: 'dataseek-visualization-browser-boundary', enforce: 'pre',
    resolveId(source, importer) {
      if (source === 'xmlbuilder2' && importer?.replace(/\\/g, '/').endsWith('/@kitware/vtk.js/IO/XML/XMLReader.js')) return resolve('src/visualizations/extended/scientific/vtkBrowserXml.ts');
      // NMRium 0.60 has one legacy deep import; use the same upstream icon's
      // ESM build, not a broad export-map bypass or a second React copy.
      if (source === 'cheminfo-font/lib-react-cjs/lib-react-tsx/nmr/Peaks' && importer?.replace(/\\/g, '/').includes('/nmrium/')) return resolve('node_modules/cheminfo-font/lib-react-esm/lib-react-tsx/nmr/Peaks.js');
      if (source === 'react-icons/lu' && importer?.replace(/\\/g, '/').includes('/nmrium/')) return resolve('src/visualizations/extended/scientific/nmriumIcons.ts');
      if (source === '@resvg/resvg-js' && importer?.replace(/\\/g, '/').includes('/jsroot/')) throw new Error('JSROOT server image export must not enter the browser bundle; use the fixed local browser asset.');
      return null;
    },
  };
}
