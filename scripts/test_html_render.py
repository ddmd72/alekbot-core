"""
Quick smoke test for PlaywrightHtmlRenderer.

Usage:
    python scripts/test_html_render.py
    open /tmp/html_card_test.png
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.adapters.playwright_html_renderer import PlaywrightHtmlRenderer

SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;background:#0f0f0f;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="padding:20px;max-width:480px;">

    <div style="background:#1a1a2e;border-radius:12px;padding:20px;border:1px solid #16213e;">
      <div style="color:#a0aec0;font-size:12px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">
        Kyiv, Ukraine
      </div>
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;">
        <div style="font-size:48px;">🌤</div>
        <div>
          <div style="color:#fff;font-size:36px;font-weight:700;">+3°C</div>
          <div style="color:#a0aec0;font-size:14px;">Partly cloudy</div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;">
        <div style="background:#16213e;border-radius:8px;padding:10px;text-align:center;">
          <div style="color:#a0aec0;font-size:11px;">Humidity</div>
          <div style="color:#63b3ed;font-size:18px;font-weight:600;">72%</div>
        </div>
        <div style="background:#16213e;border-radius:8px;padding:10px;text-align:center;">
          <div style="color:#a0aec0;font-size:11px;">Wind</div>
          <div style="color:#63b3ed;font-size:18px;font-weight:600;">14 km/h</div>
        </div>
        <div style="background:#16213e;border-radius:8px;padding:10px;text-align:center;">
          <div style="color:#a0aec0;font-size:11px;">Feels like</div>
          <div style="color:#63b3ed;font-size:18px;font-weight:600;">−1°C</div>
        </div>
      </div>
    </div>

  </div>
</body>
</html>
"""

async def main():
    renderer = PlaywrightHtmlRenderer()
    print("Rendering HTML...")
    png = await renderer.render(SAMPLE_HTML, width=480)
    await renderer.stop()

    out = "/tmp/html_card_test.png"
    with open(out, "wb") as f:
        f.write(png)

    print(f"Done: {len(png):,} bytes → {out}")
    print("Run:  open /tmp/html_card_test.png")

asyncio.run(main())
