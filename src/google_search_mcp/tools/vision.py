"""Google vision tools — lens, lens_detect, list_images, ocr_image.

Extracted from the monolithic server.py. Registers its tools on the shared
``mcp`` instance (imported from ``..server``) via ``@mcp.tool()``.
"""

import asyncio
import base64
import os
import re
from urllib.parse import quote_plus

from mcp.server.fastmcp import Context, Image
from playwright.async_api import async_playwright

from ..config import (
    DEFAULT_IMAGE_DIR,
    IMAGE_EXTENSIONS,
    MAX_OBJECTS,
    MOBILENET_ONNX_PATH,
)
from ..server import (
    mcp,
    browse_google,
    dismiss_consent,
    format_error,
    human_delay,
    is_blocked,
    launch_browser,
    load_cookies,
    save_cookies,
    try_solve_captcha,
)
from ..utils.image import is_base64_image, is_local_file, save_base64_image


# google_lens (reverse image search)
# ---------------------------------------------------------------------------

def is_base64_image(data: str) -> bool:
    """Check if the input looks like base64-encoded image data."""
    # data:image/png;base64,... or raw base64 (very long string, no slashes/spaces)
    if data.startswith("data:image/"):
        return True
    # Raw base64: long string without path separators, starts with typical base64 chars
    if len(data) > 200 and "/" not in data[:50] and not data.startswith(("http", "~")):
        try:
            import base64
            # Try decoding first 100 chars to verify it's valid base64
            base64.b64decode(data[:100] + "==", validate=True)
            return True
        except Exception:
            pass
    return False


def save_base64_image(data: str) -> str:
    """Save base64 image data to a temp file and return the path."""
    import base64
    import tempfile

    # Strip data URI prefix if present
    if data.startswith("data:image/"):
        # data:image/png;base64,<data>
        header, b64data = data.split(",", 1)
        mime = header.split(";")[0].split(":")[1]
        ext = mime.split("/")[1].replace("jpeg", "jpg")
    else:
        b64data = data
        ext = "png"  # default

    img_bytes = base64.b64decode(b64data)

    tmp = tempfile.NamedTemporaryFile(
        suffix=f".{ext}", prefix="mcp_img_", delete=False,
        dir=os.path.join(os.path.expanduser("~"), ".cache", "noapi-google-search-mcp"),
    )
    tmp.write(img_bytes)
    tmp.close()
    return tmp.name


def is_local_file(path: str) -> bool:
    """Check if the input looks like a local file path rather than a URL."""
    if path.startswith(("http://", "https://", "data:")):
        return False
    # Absolute or relative path, or ~ home path
    return path.startswith(("/", "~", "./", "../")) or os.path.exists(path)


async def do_google_lens(image_source: str) -> str:
    """Reverse image search using Google Lens. Supports URLs, local files, and base64."""
    # Handle base64 input (from drag-and-drop in LM Studio)
    tmp_base64_path = None
    if is_base64_image(image_source):
        os.makedirs(os.path.join(os.path.expanduser("~"), ".cache", "noapi-google-search-mcp"), exist_ok=True)
        tmp_base64_path = save_base64_image(image_source)
        image_source = tmp_base64_path

    is_local = is_local_file(image_source)

    if is_local:
        file_path = str(Path(image_source).expanduser().resolve())
        if not os.path.isfile(file_path):
            return f"File not found: {image_source}\nPlease provide a valid file path or a public image URL."

    async with async_playwright() as pw:
        context = await launch_browser(pw)
        await load_cookies(context)
        page = await context.new_page()

        try:
            if is_local:
                # Local file: go to Google Images and upload via file chooser
                await page.goto("https://images.google.com/?hl=en", wait_until="domcontentloaded", timeout=30000)
                await dismiss_consent(page)
                await page.wait_for_timeout(1000)

                # Click the camera/lens icon to open image search
                lens_btn = page.locator("[aria-label='Search by image'], .Gdd5U, .nDcEnd, .tdAaF")
                if await lens_btn.count() > 0:
                    await lens_btn.first.click()
                    await page.wait_for_timeout(1500)

                # Upload the file - Playwright file chooser approach
                file_input = page.locator("input[type='file']")
                if await file_input.count() > 0:
                    await file_input.first.set_input_files(file_path)
                else:
                    # Fallback: try drag area upload button
                    upload_btn = page.locator("a:has-text('upload a file'), span:has-text('upload a file'), div:has-text('upload a file')")
                    if await upload_btn.count() > 0:
                        async with page.expect_file_chooser() as fc_info:
                            await upload_btn.first.click()
                        file_chooser = await fc_info.value
                        await file_chooser.set_files(file_path)
                    else:
                        return "Could not find the upload button on Google Images. Try providing a public image URL instead."

                # Wait for Lens results to load
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)
                await dismiss_consent(page)

            else:
                # URL-based: use uploadbyurl
                encoded_url = quote_plus(image_source)
                url = f"https://lens.google.com/uploadbyurl?url={encoded_url}&hl=en"
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await dismiss_consent(page)
                await page.wait_for_timeout(2000)

            # Click "Change to English" if present
            try:
                eng_link = page.locator("a:has-text('Change to English'), a:has-text('English')")
                if await eng_link.count() > 0:
                    await eng_link.first.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    await dismiss_consent(page)
            except Exception:
                pass

            # Lens takes time to process the image
            await page.wait_for_timeout(4000)

            # Detect and handle CAPTCHA/rate-limit blocks
            if await is_blocked(page):
                solved = await try_solve_captcha(page)
                if not solved:
                    # Warm-up retry: go to Google home first, then re-run
                    try:
                        await page.goto(
                            "https://www.google.com/ncr",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await dismiss_consent(page)
                        await human_delay(page)
                        if is_local:
                            await page.goto("https://images.google.com/?hl=en", wait_until="domcontentloaded", timeout=30000)
                            await dismiss_consent(page)
                            await page.wait_for_timeout(1000)
                            lens_btn = page.locator("[aria-label='Search by image'], .Gdd5U, .nDcEnd, .tdAaF")
                            if await lens_btn.count() > 0:
                                await lens_btn.first.click()
                                await page.wait_for_timeout(1500)
                            file_input = page.locator("input[type='file']")
                            if await file_input.count() > 0:
                                await file_input.first.set_input_files(file_path)
                            await page.wait_for_load_state("domcontentloaded", timeout=30000)
                            await page.wait_for_timeout(5000)
                            await dismiss_consent(page)
                        else:
                            encoded_url = quote_plus(image_source)
                            url = f"https://lens.google.com/uploadbyurl?url={encoded_url}&hl=en"
                            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                            await dismiss_consent(page)
                            await page.wait_for_timeout(2000)
                    except Exception:
                        pass

                    if await is_blocked(page):
                        await save_cookies(context)
                        return (
                            f"Google Lens Results for image: {image_source}\n\n"
                            f"Google is currently blocking automated requests (CAPTCHA/rate-limit).\n"
                            f"Try again in a few minutes, or use a local file with google_lens_detect instead."
                        )

            # Check for error
            page_text = await page.evaluate("() => document.body.innerText.substring(0, 500)")
            if "No image at the URL" in page_text or "Something went wrong" in page_text:
                if is_local:
                    return f"Google Lens could not process the image: {image_source}\nThe file may be corrupted or in an unsupported format."
                return f"Google Lens could not access the image at: {image_source}\nThe image URL must be publicly accessible. Try a direct image link (ending in .jpg, .png, etc.)."

            data = await page.evaluate(
                r"""
                () => {
                    const data = {
                        ai_overview: '',
                        visual_matches: [],
                        product_results: [],
                        exact_matches: []
                    };

                    // AI Overview - Google's description of the image
                    const bodyText = document.body.innerText;
                    const aiIdx = bodyText.indexOf('AI Overview');
                    if (aiIdx !== -1) {
                        // Get text after "AI Overview" until next section
                        const afterAi = bodyText.substring(aiIdx + 11, aiIdx + 1500);
                        const endMarkers = ['Visual matches', 'Exact matches', 'Products', 'Related links', 'Footer'];
                        let endIdx = afterAi.length;
                        for (const marker of endMarkers) {
                            const idx = afterAi.indexOf(marker);
                            if (idx !== -1 && idx < endIdx) endIdx = idx;
                        }
                        data.ai_overview = afterAi.substring(0, endIdx).trim();
                        // Clean up
                        if (data.ai_overview.startsWith('\n')) {
                            data.ai_overview = data.ai_overview.substring(1).trim();
                        }
                        // Remove "Dive deeper in AI Mode" suffix
                        const diveIdx = data.ai_overview.indexOf('Dive deeper');
                        if (diveIdx !== -1) {
                            data.ai_overview = data.ai_overview.substring(0, diveIdx).trim();
                        }
                    }

                    // Visual matches section - all the heading DIVs are visual match titles
                    const allHeadings = document.querySelectorAll('div[role="heading"]');
                    const skipTexts = new Set([
                        'Choose what you\'re giving feedback on',
                        'Customised date range',
                        'Search Results',
                        'Filters and topics'
                    ]);
                    for (const h of allHeadings) {
                        if (data.visual_matches.length >= 10) break;
                        const text = h.innerText.trim();
                        if (!text || text.length < 3 || skipTexts.has(text)) continue;

                        // Find parent link
                        const parentLink = h.closest('a[href]');
                        let url = '';
                        let source = '';
                        if (parentLink) {
                            url = parentLink.href || '';
                            // Source is usually the first line of the link text
                            const linkLines = parentLink.innerText.trim().split('\n');
                            if (linkLines.length > 1 && linkLines[0] !== text) {
                                source = linkLines[0];
                            }
                        }

                        // Get rating if present nearby
                        const parent = h.parentElement;
                        let rating = '';
                        if (parent) {
                            const rText = parent.innerText;
                            const rMatch = rText.match(/(\d\.\d)\([\d,]+\)/);
                            if (rMatch) rating = rMatch[0];
                        }

                        if (url && !url.includes('google.com/search')) {
                            data.visual_matches.push({
                                name: text,
                                url: url,
                                source: source,
                                rating: rating
                            });
                        }
                    }

                    // Product results with prices (h3 elements with links)
                    const h3s = document.querySelectorAll('h3');
                    for (const h3 of h3s) {
                        if (data.product_results.length >= 8) break;
                        const text = h3.innerText.trim();
                        if (!text || text.length < 5) continue;

                        const container = h3.closest('.g') || h3.parentElement?.parentElement?.parentElement;
                        if (!container) continue;

                        const linkEl = container.querySelector('a[href^="http"]');
                        const containerText = container.innerText;

                        // Look for price patterns
                        const priceMatch = containerText.match(/(?:US?\$|€|£|CHF|MX\$)\s*[\d,.]+/);
                        const snippetEl = container.querySelector('.VwiC3b, [data-sncf]');

                        if (linkEl) {
                            data.product_results.push({
                                name: text,
                                url: linkEl.href,
                                price: priceMatch ? priceMatch[0] : '',
                                snippet: snippetEl ? snippetEl.innerText.trim().substring(0, 300) : ''
                            });
                        }
                    }

                    // Fallback: get full page text if nothing else worked
                    if (!data.ai_overview && data.visual_matches.length === 0 && data.product_results.length === 0) {
                        const main = document.querySelector('[role="main"], body');
                        if (main) {
                            data.raw_text = main.innerText.substring(0, 5000);
                        }
                    }

                    return data;
                }
                """
            )

            lines = [f"Google Lens Results for image: {image_source}\n"]
            has_data = False

            if data.get("ai_overview"):
                lines.append(f"Image Description: {data['ai_overview']}")
                has_data = True

            if data.get("visual_matches"):
                lines.append("\nVisual Matches:")
                for i, m in enumerate(data["visual_matches"], 1):
                    entry = f"  {i}. {m['name']}"
                    if m.get("rating"):
                        entry += f" ({m['rating']})"
                    lines.append(entry)
                    if m.get("source"):
                        lines.append(f"     Source: {m['source']}")
                    if m.get("url"):
                        lines.append(f"     URL: {m['url']}")
                has_data = True

            if data.get("product_results"):
                lines.append("\nProduct Results:")
                for i, p in enumerate(data["product_results"], 1):
                    lines.append(f"  {i}. {p['name']}")
                    if p.get("price"):
                        lines.append(f"     Price: {p['price']}")
                    if p.get("snippet"):
                        lines.append(f"     {p['snippet']}")
                    if p.get("url"):
                        lines.append(f"     URL: {p['url']}")
                has_data = True

            if not has_data and data.get("raw_text"):
                raw = re.sub(r'\n{3,}', '\n\n', data["raw_text"]).strip()
                lines.append(raw)
                has_data = True

            if not has_data:
                lines.append("Could not identify the image. Try with a clearer image or a direct product photo.")

            return "\n".join(lines)

        except Exception as e:
            return format_error("Google Lens search", e)

        finally:
            await save_cookies(context)
            await context.close()
            # Clean up base64 temp file
            if tmp_base64_path:
                try:
                    os.remove(tmp_base64_path)
                except OSError:
                    pass


@mcp.tool()
async def google_lens(image_source: str) -> str:
    """Reverse image search using Google Lens. Identify objects, products, brands, landmarks, text in images, and find visually similar results.

    This gives vision capabilities to text-only models. Supports public image URLs,
    local file paths, and base64-encoded image data (from drag-and-drop in LM Studio).

    Sample prompts that trigger this tool:
        - "What is this product? https://example.com/photo.jpg"
        - "Identify this image: /home/user/photos/image.jpg"
        - "What is in this image?" (with image dragged into chat)
        - "What brand is this? [image URL or file path]"

    Args:
        image_source: A public image URL, local file path, or base64-encoded image data.
    """
    return await do_google_lens(image_source)


# ---------------------------------------------------------------------------
# google_lens_detect (object detection + per-object Lens identification)
# ---------------------------------------------------------------------------


def _detect_objects(image_path: str, min_area_ratio: float = 0.02) -> list[dict]:
    """Detect distinct objects in an image using OpenCV contour detection.

    Returns list of dicts with keys: x, y, w, h, label (position description).
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []

    img = cv2.imread(image_path)
    if img is None:
        return []

    h, w = img.shape[:2]
    total_area = h * w
    min_area = total_area * min_area_ratio

    # Convert to grayscale and apply edge detection
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (7, 7), 0)
    edges = cv2.Canny(blurred, 30, 100)

    # Dilate edges to close gaps
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    dilated = cv2.dilate(edges, kernel, iterations=3)

    # Find contours
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Get bounding boxes for significant contours
    boxes = []
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        area = bw * bh
        if area >= min_area and area < total_area * 0.95:
            boxes.append((x, y, bw, bh, area))

    if not boxes:
        return []

    # Sort by area descending
    boxes.sort(key=lambda b: b[4], reverse=True)

    # Merge overlapping boxes
    merged = []
    used = set()
    for i, (x1, y1, w1, h1, a1) in enumerate(boxes):
        if i in used:
            continue
        mx, my, mw, mh = x1, y1, w1, h1
        for j, (x2, y2, w2, h2, a2) in enumerate(boxes):
            if j <= i or j in used:
                continue
            # Check overlap
            ox = max(0, min(mx + mw, x2 + w2) - max(mx, x2))
            oy = max(0, min(my + mh, y2 + h2) - max(my, y2))
            overlap = ox * oy
            smaller_area = min(mw * mh, w2 * h2)
            if smaller_area > 0 and overlap / smaller_area > 0.3:
                # Merge
                nx = min(mx, x2)
                ny = min(my, y2)
                mw = max(mx + mw, x2 + w2) - nx
                mh = max(my + mh, y2 + h2) - ny
                mx, my = nx, ny
                used.add(j)
        merged.append((mx, my, mw, mh))
        used.add(i)

    # Add padding (10%) and generate position labels
    results = []
    for mx, my, mw, mh in merged[:MAX_OBJECTS]:
        pad_x = int(mw * 0.1)
        pad_y = int(mh * 0.1)
        cx = max(0, mx - pad_x)
        cy = max(0, my - pad_y)
        cw = min(w - cx, mw + 2 * pad_x)
        ch = min(h - cy, mh + 2 * pad_y)

        # Position label
        cy_center = (cy + ch / 2) / h
        cx_center = (cx + cw / 2) / w
        v_pos = "top" if cy_center < 0.33 else ("middle" if cy_center < 0.66 else "bottom")
        h_pos = "left" if cx_center < 0.33 else ("center" if cx_center < 0.66 else "right")
        label = f"{v_pos}-{h_pos}"

        results.append({"x": cx, "y": cy, "w": cw, "h": ch, "label": label})

    return results


async def _lens_upload_in_session(page, file_path: str) -> str:
    """Upload a single image to Google Lens within an existing browser session.

    Navigates to images.google.com, uploads, and extracts results.
    """
    await page.goto("https://images.google.com/?hl=en", wait_until="domcontentloaded", timeout=30000)
    await dismiss_consent(page)
    await page.wait_for_timeout(1000)

    # Click the camera/lens icon
    lens_btn = page.locator("[aria-label='Search by image'], .Gdd5U, .nDcEnd, .tdAaF")
    if await lens_btn.count() > 0:
        await lens_btn.first.click()
        await page.wait_for_timeout(1500)

    # Upload the file
    file_input = page.locator("input[type='file']")
    if await file_input.count() == 0:
        return "Could not find upload input"
    await file_input.first.set_input_files(file_path)

    # Wait for results
    await page.wait_for_load_state("domcontentloaded", timeout=30000)
    await page.wait_for_timeout(5000)
    await dismiss_consent(page)

    # Click "Change to English" if needed
    try:
        eng_link = page.locator("a:has-text('Change to English'), a:has-text('English')")
        if await eng_link.count() > 0:
            await eng_link.first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            await dismiss_consent(page)
    except Exception:
        pass

    await page.wait_for_timeout(3000)

    # Check for errors
    page_text = await page.evaluate("() => document.body.innerText.substring(0, 500)")
    if "unusual traffic" in page_text.lower() or "sorry" in page_text.lower():
        return "Rate limited by Google. Try again later."
    if "No image at the URL" in page_text or "Something went wrong" in page_text:
        return "Google Lens could not process this image crop."

    # Extract results (same scraper as _do_google_lens)
    data = await page.evaluate(
        r"""
        () => {
            const data = { ai_overview: '', visual_matches: [], product_results: [] };

            const bodyText = document.body.innerText;
            const aiIdx = bodyText.indexOf('AI Overview');
            if (aiIdx !== -1) {
                const afterAi = bodyText.substring(aiIdx + 11, aiIdx + 1500);
                const endMarkers = ['Visual matches', 'Exact matches', 'Products', 'Related links', 'Footer'];
                let endIdx = afterAi.length;
                for (const marker of endMarkers) {
                    const idx = afterAi.indexOf(marker);
                    if (idx !== -1 && idx < endIdx) endIdx = idx;
                }
                data.ai_overview = afterAi.substring(0, endIdx).trim();
                if (data.ai_overview.startsWith('\n')) data.ai_overview = data.ai_overview.substring(1).trim();
                const diveIdx = data.ai_overview.indexOf('Dive deeper');
                if (diveIdx !== -1) data.ai_overview = data.ai_overview.substring(0, diveIdx).trim();
            }

            const allHeadings = document.querySelectorAll('div[role="heading"]');
            const skipTexts = new Set(['Choose what you\'re giving feedback on', 'Customised date range', 'Search Results', 'Filters and topics']);
            for (const h of allHeadings) {
                if (data.visual_matches.length >= 5) break;
                const text = h.innerText.trim();
                if (!text || text.length < 3 || skipTexts.has(text)) continue;
                const parentLink = h.closest('a[href]');
                let url = '', source = '';
                if (parentLink) {
                    url = parentLink.href || '';
                    const linkLines = parentLink.innerText.trim().split('\n');
                    if (linkLines.length > 1 && linkLines[0] !== text) source = linkLines[0];
                }
                if (url && !url.includes('google.com/search')) {
                    data.visual_matches.push({ name: text, url: url, source: source });
                }
            }

            if (!data.ai_overview && data.visual_matches.length === 0) {
                const main = document.querySelector('[role="main"], body');
                if (main) data.raw_text = main.innerText.substring(0, 3000);
            }

            return data;
        }
        """
    )

    lines = []
    if data.get("ai_overview"):
        lines.append(f"Identification: {data['ai_overview']}")
    if data.get("visual_matches"):
        lines.append("Visual Matches:")
        for i, m in enumerate(data["visual_matches"][:3], 1):
            entry = f"  {i}. {m['name']}"
            if m.get("source"):
                entry += f" ({m['source']})"
            lines.append(entry)
            if m.get("url"):
                lines.append(f"     {m['url']}")
    if not lines and data.get("raw_text"):
        raw = re.sub(r'\n{3,}', '\n\n', data["raw_text"]).strip()[:1000]
        lines.append(raw)
    if not lines:
        lines.append("Could not identify this object.")

    return "\n".join(lines)


async def do_google_lens_detect(image_path: str) -> str:
    """Detect objects in an image and identify each via Google Lens."""
    try:
        import cv2
    except ImportError:
        return "opencv-python-headless is required for object detection. Install with: pip install opencv-python-headless"

    file_path = str(Path(image_path).expanduser().resolve())
    if not os.path.isfile(file_path):
        return f"File not found: {image_path}"

    # Detect objects
    objects = _detect_objects(file_path)

    # Create temp crops
    import tempfile
    img = cv2.imread(file_path)
    if img is None:
        return f"Could not read image: {file_path}"

    crop_files = []
    temp_dir = tempfile.mkdtemp(prefix="lens_detect_")
    try:
        for i, obj in enumerate(objects):
            crop = img[obj["y"]:obj["y"] + obj["h"], obj["x"]:obj["x"] + obj["w"]]
            crop_path = os.path.join(temp_dir, f"object_{i}_{obj['label']}.jpg")
            cv2.imwrite(crop_path, crop)
            crop_files.append((crop_path, obj["label"]))

        if not crop_files:
            # Fallback: no objects detected, just pass original
            return await do_google_lens(file_path)

        # Run Lens on original + each crop in a single browser session
        async with async_playwright() as pw:
            context = await launch_browser(pw)
            page = await context.new_page()

            results = []

            try:
                # First: original full image
                og_result = await _lens_upload_in_session(page, file_path)
                results.append(("Full image (original)", og_result))
                await page.wait_for_timeout(3000)

                # Then: each detected object crop
                for crop_path, label in crop_files:
                    crop_result = await _lens_upload_in_session(page, crop_path)
                    results.append((f"Object ({label})", crop_result))
                    await page.wait_for_timeout(3000)

            except Exception as e:
                results.append(("Error", str(e)))

            finally:
                await context.close()

        # Format output
        lines = [
            f"Google Lens Object Detection Results",
            f"Image: {image_path}",
            f"Objects detected: {len(crop_files)}",
            ""
        ]
        for label, result in results:
            lines.append(f"--- {label} ---")
            lines.append(result)
            lines.append("")

        return "\n".join(lines)

    finally:
        # Clean up temp files
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


@mcp.tool()
async def google_lens_detect(image_source: str) -> str:
    """Detect and identify all objects in an image using OpenCV object detection and Google Lens.

    Unlike google_lens which sends the full image, this tool:
    1. Uses OpenCV to detect distinct objects/regions in the image
    2. Crops each object separately
    3. Sends the original image AND each crop to Google Lens
    4. Returns identification results for each object

    This is useful when an image contains multiple items (e.g. a monitor AND a hardware device)
    and you want each identified separately.

    Supports local file paths and base64-encoded image data (from drag-and-drop).

    Sample prompts that trigger this tool:
        - "Detect and identify all objects in this image: /path/to/photo.jpg"
        - "What are all the items in this photo?" (with image dragged into chat)
        - "Identify each object separately in /path/to/setup.jpg"

    Args:
        image_source: Local file path or base64-encoded image data.
    """
    # Handle base64 input
    if is_base64_image(image_source):
        os.makedirs(os.path.join(os.path.expanduser("~"), ".cache", "noapi-google-search-mcp"), exist_ok=True)
        image_source = save_base64_image(image_source)
    elif image_source.startswith(("http://", "https://")):
        return "google_lens_detect only works with local files. Use google_lens for URLs."
    return await do_google_lens_detect(image_source)


# ---------------------------------------------------------------------------
# list_images (helper for text-only models to discover local images)
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".tiff", ".tif"}
DEFAULT_IMAGE_DIR = os.path.expanduser("~/lens")


@mcp.tool()
async def list_images(directory: str = "") -> str:
    """List image files in a directory so you can pass them to google_lens.

    This is useful for text-only models that cannot receive images directly.
    The user saves an image to ~/lens/ (or any folder) and asks you to identify it.

    Default directory: ~/lens/

    Sample prompts that trigger this tool:
        - "What images are in my lens folder?"
        - "Identify the latest image"
        - "Check ~/lens/ for new images"
        - "What did I save?"

    Args:
        directory: Folder to scan for images. Defaults to ~/lens/.
    """
    scan_dir = directory.strip() if directory.strip() else DEFAULT_IMAGE_DIR
    scan_dir = str(Path(scan_dir).expanduser().resolve())

    if not os.path.isdir(scan_dir):
        return f"Directory not found: {scan_dir}\nCreate it with: mkdir -p ~/lens\nThen save images there for identification."

    files = []
    for f in os.listdir(scan_dir):
        ext = os.path.splitext(f)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            full_path = os.path.join(scan_dir, f)
            stat = os.stat(full_path)
            files.append((f, full_path, stat.st_mtime, stat.st_size))

    if not files:
        return f"No images found in {scan_dir}\nSupported formats: {', '.join(sorted(IMAGE_EXTENSIONS))}"

    # Sort by modification time, newest first
    files.sort(key=lambda x: x[2], reverse=True)

    lines = [f"Images in {scan_dir} ({len(files)} found):\n"]
    for name, path, mtime, size in files:
        dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        size_kb = size / 1024
        lines.append(f"  {name}")
        lines.append(f"    Path: {path}")
        lines.append(f"    Modified: {dt} | Size: {size_kb:.0f} KB")

    lines.append(f"\nTo identify an image, use google_lens with the file path above.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ocr_image (local OCR using RapidOCR - no internet needed)
# ---------------------------------------------------------------------------

@mcp.tool()
async def ocr_image(image_source: str) -> str:
    """Extract text from an image using local OCR. No internet connection needed.

    Uses RapidOCR (PaddleOCR models on ONNX Runtime) to read text from
    screenshots, documents, photos of signs, labels, receipts, or any image
    containing text. Runs entirely locally.

    Supports local file paths and base64-encoded image data (from drag-and-drop).

    Sample prompts that trigger this tool:
        - "Read the text in this image: /path/to/image.jpg"
        - "OCR this screenshot" (with image dragged into chat)
        - "What does this document say? /path/to/document.jpg"
        - "Extract text from this image" (with image dragged into chat)

    Args:
        image_source: Local file path or base64-encoded image data.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return "rapidocr-onnxruntime is required for OCR. Install with: pip install rapidocr-onnxruntime"

    # Handle base64 input
    tmp_base64_path = None
    try:
        if is_base64_image(image_source):
            os.makedirs(os.path.join(os.path.expanduser("~"), ".cache", "noapi-google-search-mcp"), exist_ok=True)
            tmp_base64_path = save_base64_image(image_source)
            image_source = tmp_base64_path

        file_path = str(Path(image_source).expanduser().resolve())
        if not os.path.isfile(file_path):
            return f"File not found: {image_source}\nPlease provide a valid file path."

        engine = RapidOCR()
        result, elapse = engine(file_path)

        if not result:
            return f"No text found in image: {image_source}"

        # Sort by vertical position (top to bottom) then left to right
        # Each result is [bounding_box, text, confidence]
        sorted_results = sorted(result, key=lambda r: (
            min(p[1] for p in r[0]),  # min Y of bounding box
            min(p[0] for p in r[0]),  # min X of bounding box
        ))

        lines = [f"OCR Results for: {image_source}"]
        lines.append(f"Text regions found: {len(sorted_results)}")
        lines.append("")

        # Group text by approximate vertical position into lines
        text_lines = []
        current_line_texts = []
        prev_y = None
        line_threshold = 15  # pixels threshold for same-line grouping

        for box, text, confidence in sorted_results:
            min_y = min(p[1] for p in box)
            if prev_y is not None and abs(min_y - prev_y) > line_threshold:
                if current_line_texts:
                    text_lines.append(" ".join(current_line_texts))
                current_line_texts = []
            current_line_texts.append(text)
            prev_y = min_y

        if current_line_texts:
            text_lines.append(" ".join(current_line_texts))

        lines.append("--- Extracted Text ---")
        for tl in text_lines:
            lines.append(tl)

        # Also provide raw results with confidence for detailed analysis
        lines.append("")
        lines.append("--- Detailed Results (with confidence) ---")
        for box, text, confidence in sorted_results:
            lines.append(f"[{confidence:.0%}] {text}")

        det_time, cls_time, rec_time = elapse
        lines.append(f"\nProcessing time: detection={det_time:.2f}s, recognition={rec_time:.2f}s")

        return "\n".join(lines)

    except Exception as e:
        return format_error("OCR", e)
    finally:
        if tmp_base64_path and os.path.exists(tmp_base64_path):
            os.unlink(tmp_base64_path)

