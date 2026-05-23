import base64

from backend_lib import image_generation_service as service


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def test_openai_image_generation_uses_configured_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        image = base64.b64encode(b"fake-image").decode("ascii")
        return _FakeResponse({"data": [{"b64_json": image, "revised_prompt": "revised"}]})

    monkeypatch.setattr(service.requests, "post", fake_post)

    result = service.generate_image_from_config(
        {
            "models": {
                "gpt_image_2": {
                    "base_url": "https://proxy.example/v1",
                    "api_key": "sk-test",
                    "model": "gpt-image-2",
                },
            },
        },
        {
            "model_id": "gpt_image_2",
            "prompt": "一张产品图",
            "aspect_ratio": "16:9",
            "canvas_quality": "standard",
            "quality": "high",
            "output_format": "webp",
        },
    )

    assert result["ok"] is True
    assert result["model_id"] == "gpt_image_2"
    assert captured["url"] == "https://proxy.example/v1/images/generations"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "gpt-image-2"
    assert captured["json"]["quality"] == "high"
    assert captured["json"]["output_format"] == "webp"
    assert captured["json"]["size"].endswith("x1024")
    assert result["images"][0]["data_url"].startswith("data:image/webp;base64,")
    assert result["images"][0]["revised_prompt"] == "revised"


def test_gemini_image_generation_reads_inline_data(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        image = base64.b64encode(b"fake-gemini-image").decode("ascii")
        return _FakeResponse({
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"inlineData": {"mimeType": "image/png", "data": image}},
                        ],
                    },
                },
            ],
        })

    monkeypatch.setattr(service.requests, "post", fake_post)

    result = service.generate_image_from_config(
        {
            "models": {
                "nano_banana_2": {
                    "base_url": "https://generativelanguage.googleapis.com/v1beta",
                    "api_key": "gemini-key",
                    "model": "nano-banana-2",
                },
            },
        },
        {
            "model_id": "nano_banana_2",
            "prompt": "一个海报背景",
            "aspect_ratio": "3:4",
            "canvas_quality": "hd",
            "quality": "medium",
            "output_format": "png",
        },
    )

    assert result["ok"] is True
    assert result["model_id"] == "nano_banana_2"
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/models/nano-banana-2:generateContent?key=gemini-key"
    assert captured["headers"]["x-goog-api-key"] == "gemini-key"
    assert captured["json"]["contents"][0]["parts"][0]["text"] == "一个海报背景"
    assert captured["json"]["generationConfig"]["imageConfig"]["aspectRatio"] == "3:4"
    assert captured["json"]["generationConfig"]["imageConfig"]["imageSize"] == "2K"
    assert result["images"][0]["data_url"].startswith("data:image/png;base64,")


def test_openai_endpoint_accepts_base_url_with_trailing_slash():
    assert (
        service._openai_endpoint("https://proxy.example/v1/")
        == "https://proxy.example/v1/images/generations"
    )


def test_image_generation_config_is_masked():
    result = service.image_generation_config_to_api(
        {
            "models": {
                "gpt_image_2": {
                    "base_url": "https://proxy.example/v1",
                    "api_key": "secret-key",
                    "model": "gpt-image-2",
                },
            },
        },
        lambda value: "***" + str(value)[-3:] if value else "",
    )

    gpt_image = next(model for model in result["models"] if model["id"] == "gpt_image_2")
    nano_banana = next(model for model in result["models"] if model["id"] == "nano_banana_2")
    assert result["default_model_id"] == "gpt_image_2"
    assert gpt_image["configured"] is True
    assert gpt_image["has_key"] is True
    assert gpt_image["model"] == "gpt-image-2"
    assert nano_banana["configured"] is False
