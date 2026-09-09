import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import monacoEditorPlugin from 'vite-plugin-monaco-editor';
import { resolve } from 'path';
import { visualizationAssets } from './visualization-assets';
import { visualizationBrowserBoundary } from './visualization-browser-boundary';

// https://vitejs.dev/config/
export default defineConfig({
  build: {
    target: 'esnext',
  },
  plugins: [
    visualizationBrowserBoundary(),
    vue(),
    visualizationAssets(),
    (monacoEditorPlugin as any).default({})
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  optimizeDeps: {
    exclude: ['lucide-vue-next'],
    esbuildOptions: {
      target: 'esnext',
    },
  },
  server: {
    host: true,
    port: 5173,
    ...(process.env.BACKEND_URL && {
      proxy: {
        '/api': {
          target: process.env.BACKEND_URL,
          changeOrigin: true,
          ws: true,
        },
      },
    }),
  },
}); 
