import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'recovery.fixture.spec.mjs',
  timeout: 45000,
  workers: 1,
  retries: 0,
  outputDir: 'test-results',
  use: { viewport: { width: 1100, height: 900 }, trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    { name: 'webkit', use: { browserName: 'webkit' } },
  ],
});
