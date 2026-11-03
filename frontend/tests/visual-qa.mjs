import { chromium } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { mockApi } from './fixtures.ts';

const output = resolve('../backend/.artifacts/frontend-qa-20261101');
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
await mockApi(page);
const reports = [];
try {
  for (const route of ['/', '/shop', '/dashboard', '/voice']) {
    await page.goto(`http://127.0.0.1:5173${route}`);
    await page.getByRole('heading', { level: 1 }).waitFor();
    await page.evaluate(() => document.fonts.ready);
    if (route === '/') {
      await page.locator('canvas').waitFor({ state: 'visible', timeout: 15000 }).catch(() => {});
      await page.screenshot({ path: resolve(output, 'desktop-landing-normal-motion.png'), timeout: 10000 });
    }
    const report = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
    const violations = report.violations.map(({ id, impact, description, nodes }) => ({
      id, impact, description,
      nodes: nodes.map(({ target, failureSummary, html }) => ({ target, failureSummary, html })),
    }));
    reports.push({ route, violations });
    console.log(JSON.stringify({ route, violations }));
  }
  await page.setViewportSize({ width: 412, height: 915 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('http://127.0.0.1:5173/dashboard');
  await page.getByRole('heading', { level: 1 }).waitFor();
  await page.screenshot({ path: resolve(output, 'mobile-dashboard.png'), fullPage: true, timeout: 10000 });
} finally {
  await writeFile(resolve(output, 'axe-results.json'), JSON.stringify(reports, null, 2));
  await browser.close();
}
