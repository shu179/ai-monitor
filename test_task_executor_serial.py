from core.task_executor_serial import SerialPlatformRuntime


class FakePlatform:
    def __init__(self, name: str, *, page=object(), fail_close: bool = False) -> None:
        self.name = name
        self.page = page
        self.closed = False
        self.started = False
        self.fail_close = fail_close

    def start(self):
        self.started = True
        return self

    def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


def test_serial_runtime_acquires_and_reuses_live_platform():
    calls = []

    def create_platform(platform_name, platform_class, *, config, inspect, stop_checker):
        calls.append((platform_name, platform_class, config, inspect, stop_checker))
        return FakePlatform(platform_name)

    stop_checker = object()
    runtime = SerialPlatformRuntime(
        {"name": "", "platform": None},
        task={"inspect": True},
        config={"x": 1},
        stop_checker=stop_checker,
        create_browser_platform=create_platform,
        logger=lambda _message: None,
    )

    first = runtime.acquire("doubao", "PlatformClass")
    second = runtime.acquire("doubao", "PlatformClass")

    assert first is second
    assert first.started
    assert runtime.state["name"] == "doubao"
    assert calls == [("doubao", "PlatformClass", {"x": 1}, True, stop_checker)]


def test_serial_runtime_closes_platform_when_switching():
    created = []

    def create_platform(platform_name, platform_class, *, config, inspect, stop_checker):
        del platform_class, config, inspect, stop_checker
        platform = FakePlatform(platform_name)
        created.append(platform)
        return platform

    runtime = SerialPlatformRuntime(
        {"name": "", "platform": None},
        task={},
        config={},
        stop_checker=None,
        create_browser_platform=create_platform,
        logger=lambda _message: None,
    )

    first = runtime.acquire("doubao", object)
    second = runtime.acquire("deepseek", object)

    assert first.closed
    assert second is created[1]
    assert runtime.state == {"name": "deepseek", "platform": second}


def test_serial_runtime_rebuilds_platform_when_page_is_missing():
    created = []

    def create_platform(platform_name, platform_class, *, config, inspect, stop_checker):
        del platform_class, config, inspect, stop_checker
        platform = FakePlatform(platform_name)
        created.append(platform)
        return platform

    broken = FakePlatform("doubao", page=None)
    runtime = SerialPlatformRuntime(
        {"name": "doubao", "platform": broken},
        task={},
        config={},
        stop_checker=None,
        create_browser_platform=create_platform,
        logger=lambda _message: None,
    )

    replacement = runtime.acquire("doubao", object)

    assert broken.closed
    assert replacement is created[0]
    assert runtime.state == {"name": "doubao", "platform": replacement}


def test_serial_runtime_close_resets_state_even_when_platform_close_fails():
    messages = []
    platform = FakePlatform("doubao", fail_close=True)
    state = {"name": "doubao", "platform": platform}
    runtime = SerialPlatformRuntime(
        state,
        task={},
        config={},
        stop_checker=None,
        logger=messages.append,
    )

    runtime.close(reason="cleanup")

    assert platform.closed
    assert state == {"name": "", "platform": None}
    assert any("关闭串行平台失败" in message for message in messages)


def test_serial_runtime_tracks_owned_state_for_missing_external_state():
    runtime = SerialPlatformRuntime(
        None,
        task={},
        config={},
        stop_checker=None,
        logger=lambda _message: None,
    )

    assert runtime.owns_state
    assert runtime.state == {"name": "", "platform": None}
