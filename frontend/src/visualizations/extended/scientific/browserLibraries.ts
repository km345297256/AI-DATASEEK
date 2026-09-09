/** Fixed, package-owned browser bundles; manifests and files cannot choose URLs. */
type Plotly = typeof import('plotly.js-cartesian-dist-min').default;
type JsRoot = typeof import('jsroot/core') & typeof import('jsroot/draw');
const libraries = {
  plotly: { url: '/visualization-assets/plotly/plotly-cartesian.min.js', global: 'Plotly', version: '4.1.0' },
  jsroot: { url: '/visualization-assets/jsroot/jsroot.min.js', global: 'JSROOT', version: '7.11.1' },
} as const;
const loading = new Map<string, Promise<unknown>>();
type NmrLibrary = Pick<typeof import('./nmriumBrowserEntry'), 'mountNmr' | 'version'>;
let nmrLoading: Promise<NmrLibrary> | undefined;
export function loadNmrBrowserLibrary(signal: AbortSignal): Promise<NmrLibrary> {
  if (signal.aborted) return Promise.reject(new DOMException('Preview cancelled', 'AbortError'));
  if (!nmrLoading) {
    const source = '/visualization-assets/nmrium/nmrium.js';
    const style = new Promise<void>((resolve, reject) => {
      const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = '/visualization-assets/nmrium/nmrium.css';
      const timer = setTimeout(() => finish(new Error('本地谱图样式加载超时。')), 30000);
      function finish(error?: Error) { clearTimeout(timer); link.onload = null; link.onerror = null; if (error) { link.remove(); reject(error); } else resolve(); }
      link.onload = () => finish(); link.onerror = () => finish(new Error('本地谱图样式无法读取。')); document.head.appendChild(link);
    });
    nmrLoading = Promise.all([import(/* @vite-ignore */ source), style]).then(([library]) => {
      if (library.version !== '0.60.0' || typeof library.mountNmr !== 'function') throw new Error('本地 NMRium 版本或接口不兼容。');
      return library as NmrLibrary;
    });
    void nmrLoading.catch(() => { nmrLoading = undefined; });
  }
  const shared = nmrLoading;
  return new Promise((resolve, reject) => {
    const onAbort = () => { signal.removeEventListener('abort', onAbort); reject(new DOMException('Preview cancelled', 'AbortError')); };
    signal.addEventListener('abort', onAbort, { once: true });
    shared.then((library) => { signal.removeEventListener('abort', onAbort); if (!signal.aborted) resolve(library); }, (error) => { signal.removeEventListener('abort', onAbort); reject(error); });
  });
}
function validatedLibrary(name: keyof typeof libraries) {
  const definition = libraries[name];
  const value = (globalThis as unknown as Record<string, any>)[definition.global];
  if (!value) return undefined;
  if (typeof value.version !== 'string' || !value.version.startsWith(definition.version) || (name === 'plotly' ? typeof value.react !== 'function' || typeof value.purge !== 'function' : typeof value.draw !== 'function' || typeof value.createHistogram !== 'function' || typeof value.cleanup !== 'function')) throw new Error('本地可视化库版本或接口不兼容。');
  return value;
}
function loadShared(name: keyof typeof libraries): Promise<unknown> {
  const existing = validatedLibrary(name); if (existing) return Promise.resolve(existing);
  const pending = loading.get(name); if (pending) return pending;
  const promise = new Promise<unknown>((resolve, reject) => {
    const script = document.createElement('script'); script.src = libraries[name].url; script.async = true; script.referrerPolicy = 'no-referrer';
    const timer = setTimeout(() => finish(new Error('本地可视化库加载超时。')), 30000);
    function finish(error?: Error) {
      clearTimeout(timer); script.onload = null; script.onerror = null;
      if (error) { script.remove(); reject(error); return; }
      try { const library = validatedLibrary(name); if (!library) throw new Error('本地可视化库未正确初始化。'); resolve(library); }
      catch (reason) { script.remove(); reject(reason); }
    }
    script.onload = () => finish(); script.onerror = () => finish(new Error('无法加载本机可视化资源，请检查构建资源。'));
    document.head.appendChild(script);
  });
  loading.set(name, promise); void promise.catch(() => loading.delete(name)); return promise;
}
export function loadBrowserLibrary(name: 'plotly', signal: AbortSignal): Promise<Plotly>;
export function loadBrowserLibrary(name: 'jsroot', signal: AbortSignal): Promise<JsRoot>;
export function loadBrowserLibrary(name: keyof typeof libraries, signal: AbortSignal): Promise<any> {
  if (signal.aborted) return Promise.reject(new DOMException('Preview cancelled', 'AbortError'));
  // A cancelled viewer stops awaiting the library, but never removes another
  // viewer's shared script. Late loading may cache code, never mount a scene.
  return new Promise((resolve, reject) => {
    const onAbort = () => { signal.removeEventListener('abort', onAbort); reject(new DOMException('Preview cancelled', 'AbortError')); };
    signal.addEventListener('abort', onAbort, { once: true });
    try { loadShared(name).then((value) => { signal.removeEventListener('abort', onAbort); if (!signal.aborted) resolve(value); }, (error) => { signal.removeEventListener('abort', onAbort); reject(error); }); }
    catch (error) { signal.removeEventListener('abort', onAbort); reject(error); }
  });
}
