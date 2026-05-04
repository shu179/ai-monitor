# Satori Renderer

This package is an optional fast path for DOM text mode screenshots.

The Python app talks to `render_worker.mjs` over newline-delimited JSON. The
worker converts Markdown/plain text into a fixed Surfaced-style SVG with
Satori, rasterizes it to PNG with Sharp, and lets Python convert the result to
JPEG so existing screenshot paths keep working. resvg remains installed as a
fallback backend for environments where Sharp cannot load.

The renderer intentionally keeps the input as Markdown/plain text. It preserves
single newlines similarly to the old browser `nl2br` path, highlights the
matched brand inline, and serves emoji from local `emoji-datasource-twitter`
assets so Sharp/resvg never need to fetch remote images.

By default text screenshots render at 2x density before JPEG encoding so small
CJK text stays crisp in chat clients that scale images down. Set
`AI_MONITOR_SATORI_SCALE=1` to return to 1x output.

If Node, dependencies, fonts, or rendering fail, `platforms/html_renderer.py`
falls back to the existing Playwright renderer.
