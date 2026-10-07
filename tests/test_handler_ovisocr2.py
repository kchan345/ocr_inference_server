from __future__ import annotations

import copy

import pytest

from ocr_server.handlers import (
    OCRResponseError,
    OvisOCR2Handler,
    Region,
    available_handlers,
    get_handler,
)
from ocr_server.handlers.ovisocr2 import clean_truncated_repeats, extract_regions, filter_image_tags
from tests.conftest import CANNED_CONTENT, CANNED_RESPONSE

# Verbatim from https://huggingface.co/ATH-MaaS/OvisOCR2 (note the leading newline).
MODEL_CARD_PROMPT = (
    '\nExtract all readable content from the image in natural human reading order and output the result as a '
    'single Markdown document. For charts or images, represent them using an HTML image tag: <'
    + 'img src="images/bbox_{left}_{top}_{right}_{bottom}.jpg" />, where left, top, right, bottom are bounding box '
    'coordinates scaled to [0, 1000). Format formulas as LaTeX. Format tables as HTML: <table>...</table>. '
    'Transcribe all other text as standard Markdown. Preserve the original text without translation or paraphrasing.'
)


def test_registry_resolves_handler_case_insensitively():
    assert "ovisocr2" in available_handlers()
    assert isinstance(get_handler("OvisOCR2"), OvisOCR2Handler)
    with pytest.raises(ValueError, match="Unknown OCR handler"):
        get_handler("does-not-exist")


def test_default_prompt_matches_model_card():
    assert OvisOCR2Handler.default_prompt == MODEL_CARD_PROMPT


def test_build_payload_follows_model_card():
    payload = OvisOCR2Handler().build_payload(model="ATH-MaaS/OvisOCR2", image_url="data:image/png;base64,AAA")
    assert payload["model"] == "ATH-MaaS/OvisOCR2"
    content = payload["messages"][0]["content"]
    assert payload["messages"][0]["role"] == "user"
    assert content[0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    assert content[1] == {"type": "text", "text": MODEL_CARD_PROMPT}
    assert payload["temperature"] == 0.0
    assert payload["max_tokens"] == 16384
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["mm_processor_kwargs"] == {"images_kwargs": {"min_pixels": 448 * 448, "max_pixels": 2880 * 2880}}


def test_custom_and_blank_prompts():
    handler = OvisOCR2Handler(max_tokens=100)
    payload = handler.build_payload(model="m", image_url="u", prompt="Only the title")
    assert payload["messages"][0]["content"][1]["text"] == "Only the title"
    assert payload["max_tokens"] == 100
    blank = handler.build_payload(model="m", image_url="u", prompt="   ")
    assert blank["messages"][0]["content"][1]["text"] == MODEL_CARD_PROMPT


def test_parse_real_ovisocr2_response():
    result = OvisOCR2Handler().parse_response(CANNED_RESPONSE)
    assert result.markdown == CANNED_CONTENT.strip()
    assert result.markdown.startswith("Quarterly Sales Report")
    assert "<table" in result.markdown
    assert "<img" not in result.text_markdown
    assert result.text_markdown.endswith("Figure 1: Q3 sales by region")
    assert result.regions == [Region(94, 592, 715, 935, "images/bbox_94_592_715_935.jpg")]
    assert result.finish_reason == "stop"
    assert not result.truncated
    assert result.usage["completion_tokens"] == 247
    assert result.warnings == []


def test_region_pixel_mapping_matches_model_card():
    region = Region(94, 592, 715, 935, "images/bbox_94_592_715_935.jpg")
    assert region.to_pixels(1000, 1100, 1000) == (94, 651, 715, 1028)
    assert region.to_dict(1000, 1100, 1000)["pixels"] == [94, 651, 715, 1028]
    assert Region(0, 0, 1200, 1200, "x").to_pixels(100, 100, 1000) == (0, 0, 100, 100)


def test_filter_image_tags_is_block_based():
    text = 'A\n\n<img src="images/bbox_1_2_3_4.jpg" />\n\nB has <img src="images/bbox_1_2_3_4.jpg" /> inline'
    assert filter_image_tags(text) == 'A\n\nB has <img src="images/bbox_1_2_3_4.jpg" /> inline'


def test_extract_regions_dedupes_and_tolerates_spacing():
    text = '<img src="images/bbox_1_2_3_4.jpg" />\n\n<img src="images/bbox_1_2_3_4.jpg"/>\n\n' \
           '<img  src="images/bbox_5_6_7_8.jpg">'
    assert [r.ref for r in extract_regions(text)] == ["images/bbox_1_2_3_4.jpg", "images/bbox_5_6_7_8.jpg"]


def test_clean_truncated_repeats():
    base = "".join(f"line {i}\n" for i in range(1200))
    assert len(base) > 8000
    assert clean_truncated_repeats(base + "abcde" * 100) == base + "abcde"
    short = "abc" * 100
    assert clean_truncated_repeats(short) == short
    assert clean_truncated_repeats(base) == base


def test_truncated_and_repeated_output_warnings():
    response = copy.deepcopy(CANNED_RESPONSE)
    response["choices"][0]["message"]["content"] = CANNED_CONTENT + "\n\n" + "abcde" * 2000
    response["choices"][0]["finish_reason"] = "length"
    result = OvisOCR2Handler().parse_response(response)
    assert result.truncated
    assert result.markdown.endswith("abcde")
    assert len(result.markdown) < len(CANNED_CONTENT) + 20
    assert len(result.warnings) == 2


@pytest.mark.parametrize("bad", [{}, {"choices": []}, {"choices": [{"message": {"content": 5}}]}])
def test_malformed_response_raises(bad):
    with pytest.raises(OCRResponseError):
        OvisOCR2Handler().parse_response(bad)
