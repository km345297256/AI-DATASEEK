// Run with: node tests/browser/navigation-layout-smoke.mjs
// Renders repository templates locally with generated Tailwind CSS; no app server or API is used.
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const require = createRequire(new URL('../../package.json', import.meta.url));
const { parse, compileStyle } = require('@vue/compiler-sfc');
const { baseParse } = require('@vue/compiler-dom');
const postcss = require('postcss');
const tailwind = require('tailwindcss');
const loadConfig = require('tailwindcss/loadConfig');
let playwrightEntry = process.env.VISUALIZATION_PLAYWRIGHT_MODULE;
if (!playwrightEntry) {
  try { playwrightEntry = require.resolve('playwright'); }
  catch { playwrightEntry = join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'); }
}
const { chromium } = await import(pathToFileURL(resolve(playwrightEntry)).href);
const source = path => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8');
const panel = source('components/LeftPanel.vue');
const pages = ['ChatPage', 'HomePage', 'AdminPage', 'PluginsPage', 'DatasetManagementPage'].map(name => ({ name, source: source(`pages/${name}.vue`) }));
const config = loadConfig(fileURLToPath(new URL('../../tailwind.config.js', import.meta.url)));
const css = (await postcss([tailwind({
  ...config,
  content: [{ raw: [panel, ...pages.map(page => page.source)].join('\n'), extension: 'vue' }],
})]).process('@tailwind base; @tailwind utilities;', { from: undefined })).css;

function findToggle(node) {
  if (node.type === 1 && node.props.some(prop => prop.type === 7 && prop.name === 'on' && prop.exp?.content === 'toggleLeftPanel')) return node.loc.source;
  return (node.children || []).map(findToggle).find(Boolean);
}

const systemChrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const browser = await chromium.launch({
  executablePath: process.env.VISUALIZATION_BROWSER_EXECUTABLE || (existsSync(systemChrome) ? systemChrome : undefined),
  headless: true,
  args: ['--disable-background-networking', '--disable-component-update'],
});
try {
  const page = await browser.newPage();
  await page.route('**/*', route => route.abort());
  for (const { name, source } of pages) {
    const descriptor = parse(source).descriptor;
    const toggle = findToggle(baseParse(descriptor.template.content));
    assert.ok(toggle, `${name}: navigation toggle exists`);
    await page.setContent('<html><head></head><body><div id="app"></div></body></html>');
    await page.addStyleTag({ content: css });
    for (const style of descriptor.styles) {
      await page.addStyleTag({ content: compileStyle({ source: style.content, id: 'data-v-navigation-fixture', scoped: style.scoped }).code });
    }
    await page.addScriptTag({ path: require.resolve('vue/dist/vue.global.prod.js') });
    await page.evaluate(({ panelTemplate, toggle }) => {
      const app = window.Vue.createApp({
        __scopeId: 'data-v-navigation-fixture',
        template: `<div class="flex h-screen">${panelTemplate}<main>${toggle}</main></div>`,
        data: () => ({
          isLeftPanelShow: false, route: { path: '/' }, sessions: [], isAdmin: false,
          isListScrolled: false, isAllTasksCollapsed: false, showUserMenu: false,
          currentUser: { fullname: 'Test' }, avatarLetter: 'T',
        }),
        methods: {
          t: value => value,
          toggleLeftPanel() { this.isLeftPanelShow = !this.isLeftPanelShow; },
          handleNewTaskClick() {}, handlePluginsClick() {}, handleDatasetChatClick() {},
          handleAdminClick() {}, toggleUserMenu() {}, handleSessionDeleted() {},
          handleSessionRenamed() {}, handleListScroll() {},
        },
      });
      app.config.globalProperties.$t = value => value;
      app.mount('#app');
    }, { panelTemplate: parse(panel).descriptor.template.content, toggle });
    for (const width of [375, 639, 640, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      const openers = page.locator('button[aria-label="Open navigation"]:visible, button[aria-label="打开导航"]:visible');
      await openers.waitFor();
      assert.equal(await openers.count(), 1, `${name}/${width}: exactly one visible opener`);
      const inPage = await openers.evaluate(button => Boolean(button.closest('main')));
      assert.equal(inPage, width < 640, `${name}/${width}: opener belongs to the page on mobile, the rail on desktop`);
      await openers.focus();
      await page.keyboard.press('Enter');
      const closer = page.getByRole('button', { name: 'Close navigation', exact: true });
      await closer.waitFor({ state: 'visible' });
      assert.equal(await closer.getAttribute('aria-expanded'), 'true');
      assert.equal(await openers.count(), 0, `${name}/${width}: no visible opener while expanded`);
      await closer.focus();
      await page.keyboard.press('Space');
      await openers.waitFor({ state: 'visible' });
      console.log(`PASS ${name} width=${width}: one opener; Enter expands; Space collapses`);
    }
  }
} finally {
  await browser.close();
}
