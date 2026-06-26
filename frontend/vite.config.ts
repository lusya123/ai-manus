import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import monacoEditorPlugin from 'vite-plugin-monaco-editor';
import { resolve } from 'path';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    (monacoEditorPlugin as any).default({})
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  optimizeDeps: {
    exclude: ['lucide-vue-next'],
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) {
            return;
          }
          if (id.includes('monaco-editor')) {
            return 'vendor-monaco';
          }
          if (id.includes('lucide-vue-next')) {
            return 'vendor-icons';
          }
          if (id.includes('reka-ui') || id.includes('@floating-ui')) {
            return 'vendor-ui';
          }
          if (id.includes('@vue') || id.includes('vue-router') || id.includes('vue-i18n') || id.includes('pinia')) {
            return 'vendor-vue';
          }
          return 'vendor';
        },
      },
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
