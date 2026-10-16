import type { Router } from 'vue-router';
import { BASE_URL } from '../api/client';
import { createReleaseMonitor, isChunkLoadError, RELEASE_MESSAGES } from './releaseCompatibility.ts';
import { showInfoToast } from './toast';

/** Owned by the app, using existing notifications without changing page layout. */
export function installReleaseCompatibility(router: Router): () => void {
  const monitor = createReleaseMonitor({
    bootBuildId: document.querySelector('meta[name="dataseek-release"]')?.getAttribute('content'),
    releaseUrl: `${import.meta.env.BASE_URL}release.json`,
    contractUrl: `${BASE_URL}/config/client-contract`,
    notify: notice => showInfoToast(RELEASE_MESSAGES[notice], 12_000),
  });
  const check = () => { if (document.visibilityState !== 'hidden') void monitor.check(); };
  const assetFailed = () => monitor.assetFailed();
  const removeRoute = router.afterEach(check);
  const removeError = router.onError(error => { if (isChunkLoadError(error)) assetFailed(); });
  window.addEventListener('focus', check);
  window.addEventListener('online', check);
  window.addEventListener('vite:preloadError', assetFailed);
  document.addEventListener('visibilitychange', check);
  check();
  return () => {
    monitor.dispose(); removeRoute(); removeError();
    window.removeEventListener('focus', check);
    window.removeEventListener('online', check);
    window.removeEventListener('vite:preloadError', assetFailed);
    document.removeEventListener('visibilitychange', check);
  };
}
