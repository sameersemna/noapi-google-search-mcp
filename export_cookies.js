// ============================================================
// Google MCP Cookie Exporter
// ============================================================
// Instructions:
//   1. Log into google.com in Chrome
//   2. Open DevTools (F12) → Console tab
//   3. Paste this entire script and press Enter
//   4. A cookies.txt file will download
//   5. Repeat on youtube.com and duckduckgo.com if needed
//   6. Copy all cookies.txt files to ~/.config/google-mcp-cookies/
//
// ⚠️  LIMITATION: document.cookie cannot read HttpOnly cookies
//     (marked with 🔒 in DevTools → Application → Cookies).
//     Most Google auth/session cookies are HttpOnly.
//
//     For FULL cookie export (including HttpOnly), use the
//     "Get cookies.txt" Chrome extension instead:
//     https://chromewebstore.google.com/detail/get-cookiestxt/bgaddhkoddajcdgocldbbfleckgcbcid
//
//     Or use the enhanced version below that tries chrome.cookies API.
// ============================================================

(function() {
    'use strict';

    const domain = location.hostname;
    const now = Math.floor(Date.now() / 1000);
    const expiry = now + 365 * 86400; // 1 year from now

    // ── Try chrome.cookies API first (works in extension/service worker context) ──
    if (typeof chrome !== 'undefined' && chrome.cookies && chrome.cookies.getAll) {
        console.log('🔍 Found chrome.cookies API — attempting full export...');
        chrome.cookies.getAll({}, function(cookies) {
            if (chrome.runtime && chrome.runtime.lastError) {
                console.warn('chrome.cookies API failed:', chrome.runtime.lastError.message);
                console.log('Falling back to document.cookie...');
                exportFromDocumentCookie();
                return;
            }
            if (!cookies || cookies.length === 0) {
                console.log('No cookies from chrome.cookies API. Falling back to document.cookie...');
                exportFromDocumentCookie();
                return;
            }
            downloadNetscapeFile(cookies, domain);
        });
        return;
    }

    // ── Fallback: read from document.cookie (no HttpOnly cookies) ──
    console.log('ℹ️  chrome.cookies API not available. Using document.cookie (HttpOnly cookies will be MISSING).');
    console.log('   For full export, install: https://chromewebstore.google.com/detail/get-cookiestxt/bgaddhkoddajcdgocldbbfleckgcbcid');
    exportFromDocumentCookie();

    // ── Helper: export from document.cookie ──
    function exportFromDocumentCookie() {
        const raw = document.cookie.split(';').filter(Boolean);
        const cookies = raw.map(pair => {
            const eq = pair.indexOf('=');
            const name = eq > -1 ? pair.substring(0, eq).trim() : pair.trim();
            const value = eq > -1 ? pair.substring(eq + 1).trim() : '';
            return { name, value, domain: domain.replace(/^www\./, '.'), path: '/', secure: false, httpOnly: false };
        });
        downloadNetscapeFile(cookies, domain);
    }

    // ── Helper: build Netscape format and download ──
    function downloadNetscapeFile(cookies, srcDomain) {
        const lines = [
            '# Netscape HTTP Cookie File',
            '# https://curl.se/rfc/cookie_spec.html',
            '# Exported by Google MCP Cookie Exporter',
            `# Source: ${srcDomain}`,
            `# Date: ${new Date().toISOString()}`,
            `# Cookies: ${cookies.length}`,
            '#',
            '# domain  domain_flag  path  secure  expiry  name  value',
            '',
        ];

        for (const c of cookies) {
            if (!c.name) continue;
            const cookieDomain = c.domain || srcDomain.replace(/^www\./, '.');
            const isSecure = c.secure || c.name.startsWith('__Secure-') || c.name.startsWith('__Host-');
            lines.push([
                cookieDomain.startsWith('.') ? cookieDomain : '.' + cookieDomain.replace(/^www\./, ''),
                'TRUE',   // include subdomains
                c.path || '/',
                isSecure ? 'TRUE' : 'FALSE',
                String(c.expires && c.expires > 0 ? c.expires : expiry),
                c.name,
                c.value,
            ].join('\t'));
        }

        const content = lines.join('\n');
        const blob = new Blob([content], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'cookies.txt';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        const httpOnlyCount = cookies.filter(c => c.httpOnly).length;
        console.log(`✅ Exported ${cookies.length} cookies from ${srcDomain}`);
        if (httpOnlyCount > 0) console.log(`🔒 Includes ${httpOnlyCount} HttpOnly cookies`);
        console.log(`📁 File: cookies.txt (${(blob.size / 1024).toFixed(1)} KB)`);
        console.log('');
        console.log('📋 Next steps:');
        console.log('   mkdir -p ~/.config/google-mcp-cookies');
        console.log('   cp ~/Downloads/cookies.txt ~/.config/google-mcp-cookies/');
        console.log('');
        console.log('🌐 Also visit these domains and run this script again:');
        console.log('   - https://www.youtube.com');
        console.log('   - https://duckduckgo.com');
        console.log('   - https://accounts.google.com');
        console.log('');
        console.log('💡 Tip: Save each domain\'s cookies to a separate file:');
        console.log('   google_cookies.txt, youtube_cookies.txt, etc.');
        console.log('   The MCP server will load ALL .txt files from the folder.');
    }
})();
