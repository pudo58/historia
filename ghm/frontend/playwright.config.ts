import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  use: {baseURL:'http://127.0.0.1:4175', viewport:{width:1366,height:768}, trace:'retain-on-failure'},
  webServer:{command:'npm run dev -- --host 127.0.0.1 --port 4175',url:'http://127.0.0.1:4175',reuseExistingServer:!process.env.CI},
});
