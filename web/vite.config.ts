import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../hermes_skillopt/webui_static',
    emptyOutDir: true,
    sourcemap: false,
  },
});
