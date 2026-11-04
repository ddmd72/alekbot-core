/**
 * pdf_generator/render_html.js
 *
 * The render sandbox's HTML -> PDF / PNG renderer (UNTRUSTED_CONTENT_SANDBOX_RFC §4.5-§4.7, slice S2).
 * Fixed, never model-written. Started only by `src/adapters/sandbox_engine/render_html_child.py`, under the engine uid
 * and the child's rlimits:
 *
 *     node render_html.js <pdf|png> <width> <sandbox:0|1>     stdin = page.html, stdout = out.bin
 *
 * The HTML is model-written and therefore untrusted. Four network layers, each enough on its own for a plain page:
 *   1. JavaScript off in both modes (no script can build a URL, set a header or open a socket);
 *   2. request interception: only `data:`, `about:` and `blob:` load; every other URL is aborted, public hosts too
 *      (the render service has no reason to fetch, RFC §4.5);
 *   3. `--host-resolver-rules=MAP * ~NOTFOUND`: no host name resolves;
 *   4. a dead proxy (`127.0.0.1:9`, loopback not bypassed): nothing leaves even if interception were bypassed.
 * `pipe: true`: DevTools speaks over a pipe, so the browser opens no debugging TCP port.
 * `--disable-dev-shm-usage`: shared-memory files go to TMPDIR (the size-limited workdir volume), not /dev/shm.
 * Chromium's own sandbox is off (`--no-sandbox`) only when <sandbox> is 0: the engine decides (CHROMIUM_OWN_SANDBOX).
 *
 * pdf: the in-process `runner.js` behaviour (networkidle0, A4, printBackground, preferCSSPageSize) minus host fetching.
 * png: the in-process `PlaywrightHtmlRenderer` behaviour (viewport {width, 800} at scale 2, 8 s content wait, the reset
 * style tag, the one-child-or-body screenshot, transparent background).
 *
 * Exit codes: 0 = the bytes are on stdout; 1 = the page did not render; 2 = the browser did not launch (systemic);
 * 4 = bad arguments or empty HTML (the child refuses those first). stderr is never returned to anyone: the launcher
 * reports only its length and SHA-256.
 *
 * `require('puppeteer')` happens inside main() so tests can load `isAllowedUrl` / `launchArgs` with node alone.
 */
'use strict';

const fs = require('fs');

const EXIT_OK = 0;
const EXIT_RENDER_FAILED = 1;
const EXIT_BROWSER_UNAVAILABLE = 2;
const EXIT_BAD_REQUEST = 4;

const ALLOWED_SCHEMES = ['data:', 'about:', 'blob:'];
const MODES = ['pdf', 'png'];
const PNG_WIDTH_MIN = 16;      // src/domain/sandbox_png_frame.py
const PNG_WIDTH_MAX = 2048;
const PNG_VIEWPORT_HEIGHT = 800;
const PNG_DEVICE_SCALE_FACTOR = 2;
const PNG_CONTENT_TIMEOUT_MS = 8000;
const RESET_STYLE = 'html,body{margin:0;padding:0;height:fit-content!important}';
const CLOSE_GRACE_MS = 5000;

const BASE_ARGS = [
    '--disable-dev-shm-usage',
    '--host-resolver-rules=MAP * ~NOTFOUND',
    '--proxy-server=127.0.0.1:9',
    '--proxy-bypass-list=<-loopback>',
    '--disable-background-networking',
    '--disable-component-update',
    '--no-pings',
    '--disable-crash-reporter',
    '--disable-breakpad',
];
const NO_SANDBOX_ARGS = ['--no-sandbox', '--disable-setuid-sandbox'];

/** True only for a URL that loads nothing from a network: `data:`, `about:`, `blob:`. */
function isAllowedUrl(url) {
    if (typeof url !== 'string') return false;
    const head = url.slice(0, 8).toLowerCase();
    return ALLOWED_SCHEMES.some((scheme) => head.startsWith(scheme));
}

/** Chromium flags; `sandbox` is the child's '0' | '1'. */
function launchArgs(sandbox) {
    return sandbox === '1' ? BASE_ARGS.slice() : BASE_ARGS.concat(NO_SANDBOX_ARGS);
}

function parseArgs(argv) {
    if (argv.length !== 3) return null;
    const [mode, rawWidth, sandbox] = argv;
    if (!MODES.includes(mode) || (sandbox !== '0' && sandbox !== '1') || !/^\d{1,5}$/.test(rawWidth)) return null;
    const width = parseInt(rawWidth, 10);
    if (mode === 'png' && (width < PNG_WIDTH_MIN || width > PNG_WIDTH_MAX)) return null;
    return { mode, width, sandbox };
}

async function readStdin() {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    return Buffer.concat(chunks).toString('utf8');
}

function writeAll(bytes) {
    const buffer = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let offset = 0;
    while (offset < buffer.length) offset += fs.writeSync(1, buffer, offset, buffer.length - offset);
}

function guardRequests(page) {
    page.on('request', (request) => {
        let allowed = false;
        try {
            allowed = isAllowedUrl(request.url());
        } catch (err) {
            allowed = false;
        }
        (allowed ? request.continue() : request.abort()).catch(() => {});
    });
}

async function renderPdf(page, html) {
    await page.setContent(html, { waitUntil: 'networkidle0' });
    return page.pdf({ format: 'A4', printBackground: true, preferCSSPageSize: true });
}

async function renderPng(page, html, width) {
    await page.setViewport({ width, height: PNG_VIEWPORT_HEIGHT, deviceScaleFactor: PNG_DEVICE_SCALE_FACTOR });
    await page.setContent(html, { waitUntil: 'networkidle0', timeout: PNG_CONTENT_TIMEOUT_MS });
    // Reset browser default margins; keep body height fit-content so it wraps the widget. Background left alone.
    // Appended to <head> through DevTools (page scripts are off), as Playwright's add_style_tag does.
    await page.evaluate((css) => {
        const style = document.createElement('style');
        style.textContent = css;
        (document.head || document.documentElement).appendChild(style);
    }, RESET_STYLE);
    // A fragment (one body child) is screenshotted as that child; a full page (2+ children) as the body.
    const childCount = await page.evaluate(() => (document.body ? document.body.children.length : 0));
    let element = childCount === 1 ? await page.$('body > *:first-child') : await page.$('body');
    if (!element) element = await page.$('body');
    if (!element) throw new Error('page has no body to screenshot');
    return element.screenshot({ omitBackground: true });
}

async function closeBrowser(browser) {
    let timer;
    const grace = new Promise((resolve) => { timer = setTimeout(resolve, CLOSE_GRACE_MS); });
    try {
        await Promise.race([browser.close(), grace]);
    } catch (err) {
        process.stderr.write('render_html.js: browser close failed\n');
    } finally {
        clearTimeout(timer);
    }
}

async function main(argv) {
    const args = parseArgs(argv);
    if (!args) {
        process.stderr.write('render_html.js: usage: <pdf|png> <width> <0|1>\n');
        return EXIT_BAD_REQUEST;
    }
    const html = await readStdin();
    if (!html.trim()) {
        process.stderr.write('render_html.js: no HTML on stdin\n');
        return EXIT_BAD_REQUEST;
    }
    let browser;
    try {
        const puppeteer = require('puppeteer');
        browser = await puppeteer.launch({ headless: 'shell', pipe: true, args: launchArgs(args.sandbox) });
    } catch (err) {
        process.stderr.write('render_html.js: browser did not launch: ' + String(err) + '\n');
        return EXIT_BROWSER_UNAVAILABLE;
    }
    try {
        const page = await browser.newPage();
        await page.setJavaScriptEnabled(false);
        await page.setRequestInterception(true);
        guardRequests(page);
        const bytes = args.mode === 'pdf' ? await renderPdf(page, html) : await renderPng(page, html, args.width);
        writeAll(bytes);
        return EXIT_OK;
    } catch (err) {
        process.stderr.write('render_html.js: render failed: ' + String(err) + '\n');
        return EXIT_RENDER_FAILED;
    } finally {
        await closeBrowser(browser);
    }
}

module.exports = { isAllowedUrl, launchArgs };

if (require.main === module) {
    main(process.argv.slice(2)).then(
        (code) => process.exit(code),
        (err) => {
            process.stderr.write('render_html.js: ' + String(err) + '\n');
            process.exit(EXIT_RENDER_FAILED);
        },
    );
}
