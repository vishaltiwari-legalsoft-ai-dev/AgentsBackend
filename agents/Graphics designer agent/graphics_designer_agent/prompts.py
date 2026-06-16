"""Canonical prompt loading + integrity hashes (spec §2.1, §9.1).

Prompts are IMMUTABLE. They live as raw ``.txt`` files under ``./prompts`` and
are loaded byte-for-byte (read as bytes to avoid any newline translation). The
frozen SHA-256 baseline below is asserted by ``tests/test_prompts.py`` so any
accidental edit to a prompt fails CI.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

# Frozen integrity baseline. Regenerating these by editing a prompt is exactly
# what the test is designed to catch — do not "fix" a failing hash, restore the
# prompt instead.
CANONICAL_SHA256: dict[str, str] = {
    "stage1_gradient_A.txt": "38ff8d4a68158244259a394491228f0f0f63c1b8d9636735fac695500a4e9ec4",
    "stage1_gradient_B.txt": "55963e93462c1d3801e0e9784dd1e6d7e251a1b0a1b5753212f54097a3f35adc",
    "stage1_gradient_C.txt": "e4e0db337340dbb4660b854870bb0703b3eab0f6dd55ad598adfe659ddd79a45",
    "stage1_gradient_D.txt": "aabf1fc80c751e108b032089a2a1d802836d67faf921cd46752fcd12922dc512",
    "stage1_gradient_E.txt": "296eaedbf5f9e19f9e06e87eff36c57c828a3e2c21a1bc37206960a0b7f13376",
    "stage1_gradient_F.txt": "299f96958474d71197f654ceef7c653248e6a7961a132bbbd7122ad488c65af2",
    "stage1_gradient_G.txt": "48722e1bbbf0c2d95ecfb2cc87fd63b411f1e0482cd01768b1c3c5712bfe42b8",
    "stage1_gradient_H.txt": "023b13a82a81fa01891d548322d058add52bf061842575ad0219eac46846f647",
    "stage1_gradient_I.txt": "7d7f84752ff06f8df66dbf153e0223d195e80c8c481bbd08620c9528f9b7f5ef",
    "stage1_gradient_J.txt": "0a145259020fb46f9b157cc805030bdfea566256b722461e5c70bcf3ce48a583",
    "stage1_gradient_K.txt": "a77373cec63427b24fe5d3bd4d5cf9e308bab67e3e4de7c97d75a83643885b40",
    "stage1_gradient_L.txt": "d4b74fc6338f8ab53d93d4988be93025bcbcf8b6986871e5478afc91581336f1",
    "stage2_element_blend.txt": "e49c3458fcfbfbac87731e58f1005a8528a5502bd59738e8d642bb313c934a5e",
    "stage3_text_overlay.txt": "3330aa3b87985b0b8c92e8e5708ebfe40d2feb535c42b8213bd89e62a6257b71",
    "stage4_logo_composite.txt": "52efdb99f973ecdf524f5690fd1917cdfbe127481458f29f0d808780788d27e8",
}


def load_prompt(filename: str) -> str:
    """Return the raw prompt text, byte-for-byte (no newline translation)."""
    return (PROMPT_DIR / filename).read_bytes().decode("utf-8")


def prompt_hash(filename: str) -> str:
    return hashlib.sha256((PROMPT_DIR / filename).read_bytes()).hexdigest()


def verify_integrity() -> list[str]:
    """Return a list of human-readable problems; empty list means all good."""
    problems: list[str] = []
    for name, expected in CANONICAL_SHA256.items():
        path = PROMPT_DIR / name
        if not path.exists():
            problems.append(f"missing prompt file: {name}")
            continue
        actual = prompt_hash(name)
        if actual != expected:
            problems.append(f"prompt modified: {name} ({actual[:12]}… != {expected[:12]}…)")
    return problems
