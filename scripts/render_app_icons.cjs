// Derive install icons from the existing Wanted Sans W artwork, without network access.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { chromium } = require('playwright');

const publicRoot = path.resolve(__dirname, '../frontend/public');
const directory = path.join(publicRoot, 'assets/branding');
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex').slice(0, 12);

(async () => {
  const artwork = fs.readFileSync(path.join(directory, 'favicon-wanted-v2-55c9b0dcbc68.svg'), 'utf8');
  const browser = await chromium.launch({ headless: true });
  const icons = [];
  try {
    const page = await browser.newPage({ deviceScaleFactor: 1 });
    await page.route('**/*', route => route.abort());
    for (const [size, purpose] of [[192, 'any'], [512, 'any'], [512, 'maskable']]) {
      await page.setViewportSize({ width: size, height: size });
      await page.setContent('<!doctype html><style>html,body{margin:0;background:transparent}svg{display:block;width:100vw;height:100vh}</style>' + artwork);
      if (purpose === 'maskable') {
        await page.evaluate(() => {
          const svg = document.querySelector('svg');
          const background = svg.querySelector('rect');
          for (const name of ['rx', 'stroke', 'stroke-width']) background.removeAttribute(name);
          for (const [name, value] of Object.entries({ x: 0, y: 0, width: 64, height: 64 })) {
            background.setAttribute(name, value);
          }
          // Keep the W inside the central 80%-diameter safe circle. The opaque
          // gradient fills the whole square so platform masks have no empty edge.
          const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
          group.setAttribute('transform', 'translate(32 32) scale(.84) translate(-32 -32)');
          group.append(svg.querySelector('path'));
          svg.append(group);
        });
      }
      const bytes = await page.screenshot({ type: 'png', omitBackground: true });
      const filename = `app-wanted-${purpose}-${size}-${digest(bytes)}.png`;
      fs.writeFileSync(path.join(directory, filename), bytes);
      icons.push({ src: `/assets/branding/${filename}`, sizes: `${size}x${size}`, type: 'image/png', purpose });
    }
  } finally {
    await browser.close();
  }

  const manifest = JSON.stringify({
    id: '/',
    name: 'Personal Wiki',
    short_name: 'Wiki',
    description: 'Talk to your notes',
    start_url: '/',
    scope: '/',
    display: 'standalone',
    background_color: '#18231f',
    theme_color: '#18231f',
    prefer_related_applications: false,
    icons,
  }, null, 2) + '\n';
  // .json is served with a JSON MIME type by the existing proxy configuration.
  fs.writeFileSync(path.join(directory, 'manifest.json'), manifest);
  const link = `  <link rel="manifest" href="/assets/branding/manifest.json?v=${digest(manifest)}">`;
  for (const filename of ['index.html', 'app/index.html']) {
    const file = path.join(publicRoot, filename);
    const html = fs.readFileSync(file, 'utf8');
    const updated = /  <link rel="manifest"[^>]*>/.test(html)
      ? html.replace(/  <link rel="manifest"[^>]*>/, link)
      : html.replace('  <meta name="theme-color" content="#18231f">', `$&\n${link}`);
    if (!updated.includes(link)) throw new Error(`Manifest link was not inserted in ${filename}`);
    fs.writeFileSync(file, updated);
  }
  console.log('Generated 192/512 icons, a maskable icon, and the linked install manifest.');
})().catch(error => { console.error(error.message); process.exitCode = 1; });
