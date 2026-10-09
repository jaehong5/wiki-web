// Render the repository's generic SVG artwork without network access.
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const directory = path.resolve(__dirname, '../frontend/public/assets/branding');
  const svg = fs.readFileSync(path.join(directory, 'og-image.svg'), 'utf8');
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
    await page.route('**/*', route => route.abort());
    await page.setContent('<!doctype html><style>html,body{margin:0;width:1200px;height:630px;overflow:hidden}svg{display:block}</style>' + svg);
    await page.screenshot({ path: path.join(directory, 'og-image.png'), type: 'png' });
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
