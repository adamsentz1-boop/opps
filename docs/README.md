# Team brief

`Opportunity-Engine-Brief.pdf` is the two-page internal overview: what the engine does, how it is wired
across the home server and n8n, why it stays cheap, and what it will never do on its own.

`brief.html` is the source. Edit it and re-render with headless Chromium:

```bash
python - <<'EOF'
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page()
        await pg.goto('file://' + __import__('os').path.abspath('docs/brief.html'),
                      wait_until='networkidle')
        await pg.pdf(path='docs/Opportunity-Engine-Brief.pdf', format='Letter',
                     print_background=True,
                     margin={'top':'0','bottom':'0','left':'0','right':'0'})
        await b.close()

asyncio.run(main())
EOF
```

The page links Google Fonts. If your renderer has no network access the type silently falls back to
system fonts, so inline the woff2 files as data URIs before rendering in an offline environment.
