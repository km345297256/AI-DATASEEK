import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readFile, readdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Plugin } from 'vite';
import { CLIENT_PROTOCOL } from './src/utils/releaseCompatibility.ts';

/** Identify the actual built assets, including uncommitted source changes.
 * No Git metadata, environment values, host paths or timestamps are published.
 */
export function releaseManifest(): Plugin {
  let output = '';
  let written = false;
  return {
    name: 'dataseek-release-manifest',
    apply: 'build',
    enforce: 'post',
    configResolved(config) { output = resolve(config.root, config.build.outDir); },
    buildStart() { written = false; },
    writeBundle() { written = true; },
    closeBundle: {
      // Rollup waits for preceding parallel hooks before a sequential hook.
      // Include offline visualization copies (closeBundle) and Monaco workers
      // (writeBundle), not only the JavaScript/CSS output bundle.
      order: 'post',
      sequential: true,
      async handler() {
        if (!written) return; // Failed / in-memory builds must not alter old dist.
        const htmlPath = resolve(output, 'index.html');
        const html = (await readFile(htmlPath, 'utf8')).replace(/<meta name="dataseek-release" content="[a-f0-9]{64}">/g, '');
        if (!html.includes('</head>')) this.error('Release identity requires the built index.html head');
        const files: string[] = [];
        async function collect(directory: string, prefix = '') {
          for (const item of await readdir(directory, { withFileTypes: true })) {
            const name = `${prefix}${item.name}`;
            if (item.isDirectory()) await collect(resolve(directory, item.name), `${name}/`);
            else if (item.isFile()) { if (name !== 'release.json') files.push(name); }
            else throw new Error('Release output must contain only regular files and directories');
          }
        }
        await collect(output);
        const digest = createHash('sha256');
        for (const name of files.sort()) {
          digest.update(name).update('\0');
          if (name === 'index.html') digest.update(html);
          else for await (const chunk of createReadStream(resolve(output, name))) digest.update(chunk);
          digest.update('\0');
        }
        const buildId = digest.digest('hex');
        await writeFile(htmlPath, html.replace('</head>', `<meta name="dataseek-release" content="${buildId}"></head>`));
        await writeFile(resolve(output, 'release.json'), JSON.stringify({
          schema_version: 1, build_id: buildId, client_protocol: CLIENT_PROTOCOL,
        }));
      },
    },
  };
}
