"""CAPTCHA detection and neural-net-powered image challenge solver.

Detects Google reCAPTCHA/rate-limit blocks and attempts to solve them
using a MobileNetV2 ONNX model for image classification challenges.
"""

import math
import os
import random
import urllib.request

import cv2
import numpy as np
import onnxruntime as ort

from .config import (
    CAPTCHA_CLASS_MAP,
    CAPTCHA_MODEL_DIR,
    IMAGENET_MEAN,
    IMAGENET_STD,
    MOBILENET_ONNX_PATH,
)


async def is_blocked(page) -> bool:
    """Check if the current page is a Google CAPTCHA or rate-limit block."""
    url = page.url
    if "/sorry/" in url:
        return True
    try:
        captcha = await page.locator(
            "iframe[src*='recaptcha'], #captcha-form, "
            "form[action*='sorry'], div.g-recaptcha"
        ).count()
        if captcha > 0:
            return True
    except Exception:
        pass
    return False


async def try_solve_captcha(page) -> bool:
    """Attempt to solve reCAPTCHA: checkbox click first, then image challenge."""
    try:
        recaptcha_frame = page.frame_locator("iframe[src*='recaptcha']")
        checkbox = recaptcha_frame.locator("#recaptcha-anchor, .recaptcha-checkbox-border")
        if await checkbox.count() > 0:
            # Focus the iframe so subsequent mouse events land in it
            try:
                iframe_el = page.locator("iframe[src*='recaptcha']").first
                box = await iframe_el.bounding_box()
                if box:
                    # Click into the iframe at a non-interactive spot to
                    # give it focus before the real click.
                    await page.mouse.click(
                        box["x"] + 5, box["y"] + 5
                    )
                    await page.wait_for_timeout(random.randint(150, 350))
            except Exception:
                pass

            # Use the human_click primitive for a natural Bezier
            # approach, hover, aim, then click.
            from . import human_sim
            box = await checkbox.first.bounding_box()
            if box:
                cx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                cy = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                # Approach: move to a point ~30-50px away, then click.
                # human_click expects coordinates in the *page* frame, but
                # the reCAPTCHA checkbox lives inside an iframe. We pass
                # the iframe-relative coordinates directly to
                # page.mouse.click which Playwright handles correctly when
                # the iframe is focused.
                try:
                    angle = random.uniform(0, 2 * 3.14159)
                    radius = random.uniform(30, 55)
                    ax = cx + math.cos(angle) * radius
                    ay = cy + math.sin(angle) * radius
                    await human_sim.human_mouse_move(page, ax, ay)
                    await page.wait_for_timeout(random.randint(180, 480))
                    await human_sim.human_mouse_move(
                        page, cx, cy, duration_ms=random.randint(140, 280)
                    )
                    await page.wait_for_timeout(random.randint(100, 320))
                    await page.mouse.click(cx, cy)
                except Exception:
                    # Fallback to the old method if Bezier move fails
                    await page.mouse.click(cx, cy)
                await page.wait_for_timeout(random.randint(2000, 4000))

                if not await is_blocked(page):
                    return True

        solved = await _solve_image_challenge(page)
        if solved:
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Neural net CAPTCHA image challenge solver
# ---------------------------------------------------------------------------


def _ensure_captcha_model() -> bool:
    """Download MobileNetV2 ONNX model if not present."""
    os.makedirs(CAPTCHA_MODEL_DIR, exist_ok=True)
    if os.path.isfile(MOBILENET_ONNX_PATH):
        return True
    try:
        model_url = (
            "https://github.com/onnx/models/raw/main/validated/vision/"
            "classification/mobilenet/model/mobilenetv2-12.onnx"
        )
        req = urllib.request.Request(model_url, headers={"User-Agent": "NoAPI-MCP/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        with open(MOBILENET_ONNX_PATH, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False


def _classify_cells(cells: list[bytes], prompt_keywords: list[str]) -> list[bool]:
    """Classify image cells against CAPTCHA prompt keywords using MobileNetV2."""
    if not _ensure_captcha_model():
        return [False] * len(cells)

    session = ort.InferenceSession(MOBILENET_ONNX_PATH)
    input_name = session.get_inputs()[0].name

    target_classes: set[int] = set()
    for keyword in prompt_keywords:
        kw_lower = keyword.lower()
        for captcha_key, class_indices in CAPTCHA_CLASS_MAP.items():
            if captcha_key in kw_lower or kw_lower in captcha_key:
                target_classes.update(class_indices)

    if not target_classes:
        return [False] * len(cells)

    results: list[bool] = []
    for cell_bytes in cells:
        arr = np.frombuffer(cell_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            results.append(False)
            continue

        img = cv2.resize(img, (224, 224))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = img.astype(np.float32) / 255.0
        for c in range(3):
            img[:, :, c] = (img[:, :, c] - IMAGENET_MEAN[c]) / IMAGENET_STD[c]
        img = np.transpose(img, (2, 0, 1))
        img = np.expand_dims(img, 0)

        outputs = session.run(None, {input_name: img})
        logits = outputs[0][0]

        exp_logits = np.exp(logits - np.max(logits))
        probs = exp_logits / exp_logits.sum()

        top_indices = np.argsort(probs)[::-1][:10]
        match = any(idx in target_classes for idx in top_indices)
        target_probs = [probs[idx] for idx in target_classes if idx < len(probs)]
        if target_probs and max(target_probs) > 0.05:
            match = True

        results.append(match)

    return results


async def _solve_image_challenge(page) -> bool:
    """Attempt to solve a reCAPTCHA image challenge using MobileNetV2."""
    try:
        challenge_frame = None
        for frame in page.frames:
            if "recaptcha" in (frame.url or "") and "bframe" in (frame.url or ""):
                challenge_frame = frame
                break

        if not challenge_frame:
            return False

        prompt_el = challenge_frame.locator(
            ".rc-imageselect-desc-no-canonical, .rc-imageselect-desc, "
            ".rc-imageselect-instructions"
        )
        if await prompt_el.count() == 0:
            return False

        prompt_text = (await prompt_el.first.inner_text()).lower()
        prompt_keywords = [prompt_text]

        grid = challenge_frame.locator("table.rc-imageselect-table, .rc-imageselect-target")
        if await grid.count() == 0:
            return False

        grid_screenshot = await grid.first.screenshot()
        if not grid_screenshot:
            return False

        tiles = challenge_frame.locator("td.rc-imageselect-tile, .rc-image-tile-wrapper")
        tile_count = await tiles.count()
        grid_size = 4 if tile_count == 16 else 3

        arr = np.frombuffer(grid_screenshot, np.uint8)
        grid_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if grid_img is None:
            return False

        h, w = grid_img.shape[:2]
        cell_h, cell_w = h // grid_size, w // grid_size
        cells: list[bytes] = []
        for row in range(grid_size):
            for col in range(grid_size):
                y1, y2 = row * cell_h, (row + 1) * cell_h
                x1, x2 = col * cell_w, (col + 1) * cell_w
                cell = grid_img[y1:y2, x1:x2]
                _, cell_bytes = cv2.imencode(".png", cell)
                cells.append(cell_bytes.tobytes())

        matches = _classify_cells(cells, prompt_keywords)
        if not any(matches):
            return False

        for i, should_click in enumerate(matches):
            if should_click:
                row, col = divmod(i, grid_size)
                tile_locator = tiles.nth(i)
                if await tile_locator.count() > 0:
                    box = await tile_locator.bounding_box()
                    if box:
                        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                        await page.mouse.click(x, y)
                        await page.wait_for_timeout(random.randint(300, 700))

        await page.wait_for_timeout(random.randint(1500, 3000))

        verify_btn = challenge_frame.locator("#recaptcha-verify-button")
        if await verify_btn.count() > 0:
            await verify_btn.first.click()
            await page.wait_for_timeout(random.randint(3000, 5000))

        return not await is_blocked(page)
    except Exception:
        return False
