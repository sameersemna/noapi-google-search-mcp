"""Comprehensive stealth JavaScript injected into every page to hide automation signals.

Covers 25+ detection vectors that modern bot-detection systems check.
This is the single source of truth — import STEALTH_JS from here, not from config.py.
"""

STEALTH_JS: str = r"""
// ============================================================
// Comprehensive Stealth JS — v2.0
// Covers 25+ detection vectors used by Google, Cloudflare, etc.
// ============================================================

// ── 1. navigator.webdriver (MOST IMPORTANT) ──
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// ── 2. navigator.plugins (headless Chrome has empty array) ──
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
          description: 'Portable Document Format',
          length: 1, item: () => null, namedItem: () => null,
          [Symbol.iterator]: function*() { yield {type: 'application/pdf'}; } },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
          description: '', length: 1, item: () => null, namedItem: () => null,
          [Symbol.iterator]: function*() { yield {type: 'application/pdf'}; } },
        { name: 'Native Client', filename: 'internal-nacl-plugin',
          description: '', length: 2, item: () => null, namedItem: () => null,
          [Symbol.iterator]: function*() { yield {type: 'application/x-nacl'}; yield {type: 'application/x-pnacl'}; } },
    ],
});

// ── 3. navigator.languages ──
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// ── 4. chrome.runtime (real Chrome has this) ──
if (!window.chrome) { window.chrome = {}; }
if (!window.chrome.runtime) {
    window.chrome.runtime = {
        connect: function() {},
        sendMessage: function() {},
        onMessage: { addListener: function() {} },
        onConnect: { addListener: function() {} },
        onInstalled: { addListener: function() {} },
    };
}

// ── 5. chrome.loadTimes (deprecated but still checked) ──
if (!window.chrome.loadTimes) {
    window.chrome.loadTimes = function() {
        return {
            requestTime: 0,
            startLoadTime: 0,
            commitLoadTime: 0,
            finishDocumentLoadTime: 0,
            finishLoadTime: 0,
            firstPaintTime: 0,
            firstPaintAfterLoadTime: 0,
            navigationType: 'Other',
            wasFetchedViaSpdy: false,
            wasNpnNegotiated: false,
            npnNegotiatedProtocol: 'unknown',
            wasAlternateProtocolAvailable: false,
            connectionInfo: 'http/1.1',
        };
    };
}

// ── 6. chrome.csi ──
if (!window.chrome.csi) {
    window.chrome.csi = function() {
        return {
            onloadT: 0,
            startE: 0,
            onloadT: 0,
            pageT: 0,
            tran: 0,
        };
    };
}

// ── 7. chrome.app ──
if (!window.chrome.app) {
    window.chrome.app = {
        isInstalled: false,
        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
        getDetails: function() { return null; },
        getIsInstalled: function() { return false; },
        install: function() { return Promise.resolve(); },
    };
}

// ── 8. navigator.hardwareConcurrency ──
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

// ── 9. navigator.deviceMemory ──
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

// ── 10. navigator.maxTouchPoints ──
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

// ── 11. navigator.platform ──
Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64' });

// ── 12. navigator.vendor ──
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });

// ── 13. navigator.productSub ──
Object.defineProperty(navigator, 'productSub', { get: () => '20030107' });

// ── 14. navigator.appVersion (match the UA) ──
Object.defineProperty(navigator, 'appVersion', {
    get: () => '5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
});

// ── 15. navigator.appName ──
Object.defineProperty(navigator, 'appName', { get: () => 'Netscape' });

// ── 16. navigator.appCodeName ──
Object.defineProperty(navigator, 'appCodeName', { get: () => 'Mozilla' });

// ── 17. navigator.oscpu ──
Object.defineProperty(navigator, 'oscpu', { get: () => 'Linux x86_64' });

// ── 18. navigator.doNotTrack ──
Object.defineProperty(navigator, 'doNotTrack', { get: () => null });

// ── 19. navigator.cookieEnabled ──
Object.defineProperty(navigator, 'cookieEnabled', { get: () => true });

// ── 20. navigator.onLine ──
Object.defineProperty(navigator, 'onLine', { get: () => true });

// ── 21. Screen properties ──
Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
Object.defineProperty(screen, 'availWidth', { get: () => 1280 });
Object.defineProperty(screen, 'availHeight', { get: () => 800 });
Object.defineProperty(screen, 'width', { get: () => 1280 });
Object.defineProperty(screen, 'height', { get: () => 800 });
Object.defineProperty(screen, 'availLeft', { get: () => 0 });
Object.defineProperty(screen, 'availTop', { get: () => 0 });

// ── 22. window.outerWidth / outerHeight consistency ──
Object.defineProperty(window, 'outerWidth', { get: () => 1280 });
Object.defineProperty(window, 'outerHeight', { get: () => 800 });
Object.defineProperty(window, 'innerWidth', { get: () => 1264 });
Object.defineProperty(window, 'innerHeight', { get: () => 760 });
Object.defineProperty(window, 'screenX', { get: () => 0 });
Object.defineProperty(window, 'screenY', { get: () => 0 });
Object.defineProperty(window, 'screenLeft', { get: () => 0 });
Object.defineProperty(window, 'screenTop', { get: () => 0 });

// ── 23. WebGL vendor/renderer spoofing (CRITICAL) ──
// Headless Chrome reports "Google SwiftShader" which is a dead giveaway.
// We intercept ALL WebGL context creations by patching getContext on the
// canvas prototype, so any detection script that creates its own canvas
// gets the spoofed values.
(function() {
    const origGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, attributes) {
        const ctx = origGetContext.apply(this, arguments);
        if (ctx && (type === 'webgl' || type === 'experimental-webgl' || type === 'webgl2')) {
            const origGetParameter = ctx.getParameter.bind(ctx);
            ctx.getParameter = function(param) {
                // UNMASKED_VENDOR_WEBGL
                if (param === 37445) {
                    return 'Intel Inc.';
                }
                // UNMASKED_RENDERER_WEBGL
                if (param === 37446) {
                    return 'Intel(R) UHD Graphics 620';
                }
                // VERSION
                if (param === 7936) {
                    return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
                }
                // SHADING_LANGUAGE_VERSION
                if (param === 35724) {
                    return 'WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)';
                }
                return origGetParameter(param);
            };
        }
        return ctx;
    };
})();

// ── 24. Canvas fingerprint randomization ──
// Add subtle noise to canvas to avoid fingerprinting matching headless Chrome.
(function() {
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
        const dataUrl = origToDataURL.apply(this, arguments);
        // Only modify non-trivial canvases (skip tiny ones used for detection)
        if (this.width < 16 || this.height < 16) {
            return dataUrl;
        }
        // Add a single pixel of noise to the PNG data
        // This changes the hash without visibly affecting the image
        const parts = dataUrl.split(',');
        if (parts.length === 2 && parts[0].includes('png')) {
            const decoded = atob(parts[1]);
            if (decoded.length > 100) {
                const arr = decoded.split('');
                // Flip one bit in a non-critical position
                const pos = 50 + (this.width * 4 + 1) % (decoded.length - 100);
                arr[pos] = String.fromCharCode(decoded.charCodeAt(pos) ^ 1);
                parts[1] = btoa(arr.join(''));
                return parts.join(',');
            }
        }
        return dataUrl;
    };
})();

// ── 25. AudioContext fingerprint randomization ──
(function() {
    const origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(channel) {
        const data = origGetChannelData.apply(this, arguments);
        if (data && data.length > 0) {
            // Add imperceptible noise to the first sample
            data[0] += 0.000001 * (Math.random() - 0.5);
        }
        return data;
    };
})();

// ── 26. navigator.connection (NetworkInformation) ──
if (navigator.connection) {
    Object.defineProperty(navigator.connection, 'effectiveType', { get: () => '4g' });
    Object.defineProperty(navigator.connection, 'rtt', { get: () => 50 });
    Object.defineProperty(navigator.connection, 'downlink', { get: () => 10 });
    Object.defineProperty(navigator.connection, 'saveData', { get: () => false });
}

// ── 27. navigator.mediaDevices stub ──
if (!navigator.mediaDevices) {
    navigator.mediaDevices = {};
}
if (!navigator.mediaDevices.enumerateDevices) {
    navigator.mediaDevices.enumerateDevices = function() {
        return Promise.resolve([
            { deviceId: 'default', kind: 'audioinput', label: '', groupId: 'default' },
            { deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default' },
            { deviceId: 'default', kind: 'videoinput', label: '', groupId: 'default' },
        ]);
    };
}

// ── 28. Battery API stub ──
if (navigator.getBattery) {
    const batteryStub = Promise.resolve({
        charging: true,
        chargingTime: 0,
        dischargingTime: Infinity,
        level: 1.0,
        onchargingchange: null,
        onchargingtimechange: null,
        ondischargingtimechange: null,
        onlevelchange: null,
    });
    navigator.getBattery = function() { return batteryStub; };
}

// ── 29. Permissions query patch ──
if (navigator.permissions && navigator.permissions.query) {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) => {
        if (params.name === 'notifications') {
            return Promise.resolve({ state: 'default', onchange: null });
        }
        if (params.name === 'clipboard-read' || params.name === 'clipboard-write') {
            return Promise.resolve({ state: 'granted', onchange: null });
        }
        if (params.name === 'geolocation') {
            return Promise.resolve({ state: 'prompt', onchange: null });
        }
        if (params.name === 'camera' || params.name === 'microphone') {
            return Promise.resolve({ state: 'prompt', onchange: null });
        }
        return origQuery(params);
    };
}

// ── 30. Performance timing consistency ──
if (window.performance && window.performance.timing) {
    const now = Date.now();
    const navStart = window.performance.timing.navigationStart || now;
    // Ensure timing values are consistent (not all zero which is suspicious)
    if (window.performance.timing.domLoading === 0) {
        try {
            Object.defineProperty(window.performance.timing, 'domLoading', {
                get: () => navStart + 50
            });
        } catch(e) {}
    }
}

// ── 31. navigator.keyboard stub ──
if (!navigator.keyboard) {
    navigator.keyboard = {
        getLayoutMap: function() { return Promise.resolve(new Map()); },
        lock: function() { return Promise.resolve(); },
        unlock: function() {},
    };
}

// ── 32. navigator.virtualKeyboard stub ──
if (!navigator.virtualKeyboard) {
    navigator.virtualKeyboard = {
        boundingRect: { x: 0, y: 0, width: 0, height: 0 },
        overlaysContent: false,
        addEventListener: function() {},
        removeEventListener: function() {},
    };
}

// ── 33. navigator.userAgentData (Client Hints) ──
// Only patch if the API exists (varies by Chromium version)
if (navigator.userAgentData) {
    try {
        Object.defineProperty(navigator.userAgentData, 'brands', {
            get: () => [
                { brand: 'Chromium', version: '149' },
                { brand: 'Google Chrome', version: '149' },
                { brand: 'Not;A=Brand', version: '24' },
            ]
        });
    } catch(e) {}
    try {
        Object.defineProperty(navigator.userAgentData, 'mobile', { get: () => false });
    } catch(e) {}
    try {
        Object.defineProperty(navigator.userAgentData, 'platform', { get: () => 'Linux' });
    } catch(e) {}
    try {
        if (navigator.userAgentData.getHighEntropyValues) {
            const origGetHighEntropy = navigator.userAgentData.getHighEntropyValues.bind(navigator.userAgentData);
            navigator.userAgentData.getHighEntropyValues = function(hints) {
                return origGetHighEntropy(hints).then(function(result) {
                    if (!result.platform) result.platform = 'Linux';
                    if (!result.platformVersion) result.platformVersion = '6.8.0';
                    if (!result.model) result.model = '';
                    if (!result.architecture) result.architecture = 'x86';
                    if (!result.bitness) result.bitness = '64';
                    if (!result.fullVersionList) {
                        result.fullVersionList = [
                            { brand: 'Chromium', version: '149.0.0.0' },
                            { brand: 'Google Chrome', version: '149.0.0.0' },
                            { brand: 'Not;A=Brand', version: '24.0.0.0' },
                        ];
                    }
                    return result;
                });
            };
        }
    } catch(e) {}
}

// ── 34. Intl.DateTimeFormat timezone consistency ──
try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!tz || tz === 'UTC') {
        // Override to a realistic timezone if the headless default is UTC
        // This is a read-only property, so we can't override it.
        // But we can note that if the timezone is UTC, it's suspicious.
    }
} catch(e) {}

// ── 35. Remove Playwright-specific properties ──
delete window.__playwright;
delete window.__pw_manual;
delete window.__pw_trace;
delete window.__pw_resize;

// ── 36. Storage API consistency ──
if (navigator.storage && navigator.storage.estimate) {
    const origEstimate = navigator.storage.estimate.bind(navigator.storage);
    navigator.storage.estimate = function() {
        return origEstimate().then(function(result) {
            if (!result.usage) result.usage = 1024 * 1024 * 50; // 50MB used
            if (!result.quota) result.quota = 1024 * 1024 * 1024; // 1GB quota
            return result;
        });
    };
}

// ── 37. Error stack trace cleanup ──
// Some bot detectors check if Error.stack traces contain "playwright" or "puppeteer"
// We patch the stack getter on Error.prototype instead of replacing the constructor
(function() {
    const origStackGetter = Object.getOwnPropertyDescriptor(Error.prototype, 'stack');
    if (origStackGetter && origStackGetter.get) {
        const origGet = origStackGetter.get;
        Object.defineProperty(Error.prototype, 'stack', {
            get: function() {
                const stack = origGet.call(this);
                if (stack) {
                    return stack
                        .replace(/playwright/gi, '')
                        .replace(/puppeteer/gi, '')
                        .replace(/chromium/gi, '');
                }
                return stack;
            }
        });
    }
})();
"""
