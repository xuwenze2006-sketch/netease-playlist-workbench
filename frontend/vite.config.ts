import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  base: './',
  build: { assetsInlineLimit: 0 },
  // Each jsdom worker owns a full DOM. Bound memory pressure on desktop hosts.
  // These are functional checks, so allow shared-host scheduling headroom;
  // explicit response deadlines and all behavior assertions remain unchanged.
  test: {
    environment: 'jsdom', setupFiles: ['./src/test/setup.ts'], restoreMocks: true,
    maxWorkers: 2, testTimeout: 15000,
  },
});
