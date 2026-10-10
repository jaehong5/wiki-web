// Rasterize the bundled Wanted Sans 800 outlines with consistent font metrics.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { chromium } = require('playwright');

// Wanted Sans 1.0.3: 2048 units/em and a 1448-unit cap height. Using one font
// scale preserves stroke weight and the font's natural round-letter overshoot.
// Width-fitting each letter would make narrower glyphs much taller and heavier.
const FONT_CAP_HEIGHT = 1448;
const ICON_CAP_HEIGHT = 28; // in the 64-unit artwork canvas
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex').slice(0, 12);

async function renderAppIcons({ publicRoot, artworkFile, filenamePrefix }) {
  const directory = path.join(publicRoot, 'assets/branding');
  const artwork = fs.readFileSync(path.join(directory, artworkFile), 'utf8');
  const manifestPath = path.join(directory, 'manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const browser = await chromium.launch({ headless: true });
  const icons = [];
  let metrics;
  try {
    const page = await browser.newPage({ deviceScaleFactor: 1 });
    await page.route('**/*', route => route.abort());
    for (const [size, purpose] of [[192, 'any'], [512, 'any'], [512, 'maskable']]) {
      await page.setViewportSize({ width: size, height: size });
      await page.setContent('<!doctype html><style>html,body{margin:0;background:transparent}svg{display:block;width:100vw;height:100vh}</style>' + artwork);
      metrics = await page.evaluate(({ purpose, fontCapHeight, iconCapHeight }) => {
        const svg = document.querySelector('svg');
        const view = svg.viewBox.baseVal;
        const paths = svg.querySelectorAll(':scope > path');
        if (view.width !== 64 || view.height !== 64 || paths.length !== 1) {
          throw new Error('Expected 64-unit artwork with one Wanted Sans glyph outline.');
        }
        const letter = paths[0];
        const bounds = letter.getBBox();
        const scale = iconCapHeight / fontCapHeight;
        const width = bounds.width * scale;
        const height = bounds.height * scale;
        if (Math.hypot(width / 2, height / 2) > 64 * .4) {
          throw new Error('Glyph exceeds the maskable safe circle at the common font size.');
        }
        const tx = 32 - (bounds.x + bounds.width / 2) * scale;
        const ty = 32 + (bounds.y + bounds.height / 2) * scale;
        letter.setAttribute('transform', `translate(${tx.toFixed(8)} ${ty.toFixed(8)}) scale(${scale.toFixed(8)} ${(-scale).toFixed(8)})`);
        // Keep identical letter size and placement in regular and maskable icons.
        // Only the background changes to cover every pixel of a platform mask.
        if (purpose === 'maskable') {
          const background = svg.querySelector('rect');
          for (const name of ['rx', 'stroke', 'stroke-width']) background.removeAttribute(name);
          for (const [name, value] of Object.entries({ x: 0, y: 0, width: 64, height: 64 })) {
            background.setAttribute(name, value);
          }
        }
        return { fontCapHeight, iconCapHeight, scale, width, height, center: [32, 32] };
      }, { purpose, fontCapHeight: FONT_CAP_HEIGHT, iconCapHeight: ICON_CAP_HEIGHT });
      const bytes = await page.screenshot({ type: 'png', omitBackground: true });
      const filename = `${filenamePrefix}-${purpose}-${size}-${digest(bytes)}.png`;
      fs.writeFileSync(path.join(directory, filename), bytes);
      icons.push({ src: `/assets/branding/${filename}`, sizes: `${size}x${size}`, type: 'image/png', purpose });
    }
  } finally {
    await browser.close();
  }
  const data = JSON.stringify({ ...manifest, icons }, null, 2) + '\n';
  fs.writeFileSync(manifestPath, data);
  return { icons, metrics, manifestHash: digest(data) };
}

async function main() {
  const publicRoot = path.resolve(__dirname, '../frontend/public');
  const result = await renderAppIcons({
    publicRoot,
    artworkFile: 'favicon-wanted-v2-55c9b0dcbc68.svg',
    filenamePrefix: 'app-wanted',
  });
  const link = `  <link rel="manifest" href="/assets/branding/manifest.json?v=${result.manifestHash}">`;
  for (const filename of ['index.html', 'app/index.html']) {
    const file = path.join(publicRoot, filename);
    const html = fs.readFileSync(file, 'utf8');
    const updated = html.replace(/  <link rel="manifest"[^>]*>/, link);
    if (!updated.includes(link)) throw new Error(`Manifest link was not updated in ${filename}`);
    fs.writeFileSync(file, updated);
  }
  console.log('Generated install icons at a shared font scale:', JSON.stringify(result.metrics));
}

module.exports = { renderAppIcons };
if (require.main === module) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
