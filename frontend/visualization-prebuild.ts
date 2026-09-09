import { build } from 'esbuild';
import { resolve } from 'node:path';

/** Fixed local source and output; no manifest can add a build entry point. */
export const nmrBrowserDirectory = () => resolve('node_modules/.cache/dataseek-visualizations/nmrium');
export const docxBrowserDirectory = () => resolve('node_modules/.cache/dataseek-visualizations/docx');
export async function prebuildDocxBrowser() {
  await build({
    entryPoints: [resolve('src/visualizations/extended/docxBrowserEntry.ts')],
    outfile: resolve(docxBrowserDirectory(), 'docx.js'),
    bundle: true, format: 'iife', platform: 'browser', target: 'es2022',
    minify: true, sourcemap: false, legalComments: 'linked',
  });
}
export async function prebuildNmrBrowser() {
  await build({
    entryPoints: [resolve('src/visualizations/extended/scientific/nmriumBrowserEntry.ts')],
    outfile: resolve(nmrBrowserDirectory(), 'nmrium.js'),
    bundle: true, format: 'esm', platform: 'browser', target: 'es2020',
    minify: true, sourcemap: false, legalComments: 'linked',
    define: { 'process.env.NODE_ENV': '"production"' },
    loader: { '.woff': 'file', '.woff2': 'file', '.ttf': 'file', '.eot': 'file', '.svg': 'file' },
    assetNames: 'assets/[name]-[hash]',
    plugins: [{ name: 'nmrium-browser-compatibility', setup(context) {
      context.onResolve({ filter: /^cheminfo-font\/lib-react-cjs\/lib-react-tsx\/nmr\/Peaks$/ }, () => ({ path: resolve('node_modules/cheminfo-font/lib-react-esm/lib-react-tsx/nmr/Peaks.js') }));
      context.onResolve({ filter: /^react-icons\/lu$/ }, (args) => args.importer.replace(/\\/g, '/').includes('/nmrium/') ? { path: resolve('src/visualizations/extended/scientific/nmriumIcons.ts') } : undefined);
      context.onResolve({ filter: /LocalStorage(?:\.js)?$/ }, (args) => {
        const target = resolve(args.resolveDir, args.path).replace(/\\/g, '/').replace(/\.js$/, '');
        return target.endsWith('/node_modules/nmrium/lib/component/utility/LocalStorage')
          ? { path: resolve('src/visualizations/extended/scientific/nmriumStorage.ts') } : undefined;
      });
    } }],
  });
}
