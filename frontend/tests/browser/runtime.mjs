/** Shared, explicit browser selection. Missing engines fail the gate, never skip. */
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
export const browserEngine = process.env.DATASEEK_BROWSER_ENGINE || 'chromium';
if (!['chromium', 'webkit'].includes(browserEngine)) throw new Error(`Unsupported DATASEEK_BROWSER_ENGINE=${browserEngine}`);

export async function loadPlaywright() {
  const require = createRequire(join(frontend, 'package.json'));
  let entry = process.env.VISUALIZATION_PLAYWRIGHT_MODULE;
  if (!entry) {
    try { entry = require.resolve('playwright'); }
    catch { entry = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'); }
  }
  if (!existsSync(entry)) throw new Error('Playwright is required for the browser gate. Install it or set VISUALIZATION_PLAYWRIGHT_MODULE; see docs/frontend-browser-gate.md.');
  return { playwright: await import(pathToFileURL(resolve(entry)).href), entry };
}

export async function launchBrowser() {
  const { playwright, entry } = await loadPlaywright();
  const systemChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const options = browserEngine === 'chromium' ? {
    executablePath: process.env.VISUALIZATION_BROWSER_EXECUTABLE || (existsSync(systemChrome) ? systemChrome : undefined),
    args: ['--disable-background-networking', '--disable-component-update'],
  } : {};
  try { return await playwright[browserEngine].launch({ ...options, headless: true }); }
  catch (cause) {
    throw new Error(`Required ${browserEngine} browser could not start. Install the matching runtime with: node "${join(dirname(entry), 'cli.js')}" install ${browserEngine}. Preserve PLAYWRIGHT_BROWSERS_PATH when using a custom cache. See docs/frontend-browser-gate.md.`, { cause });
  }
}
