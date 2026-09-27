"""No-hardware tests for mlx90640_thermal_display.py.

Run with: python3.11 test_core.py
"""

from mlx90640_thermal_display import (
    HEIGHT,
    PIXEL_COUNT,
    WIDTH,
    TemperatureScale,
    build_lut,
    center_temperature,
    percentile,
    synthetic_frame,
    temperature_to_rgb,
)


def run() -> None:
    assert WIDTH == 32
    assert HEIGHT == 24
    assert PIXEL_COUNT == 768
    assert temperature_to_rgb(20.0, 20.0, 40.0) == (0, 0, 255)
    assert temperature_to_rgb(40.0, 20.0, 40.0) == (255, 0, 240)
    assert len(build_lut(20.0, 40.0)) == 256
    assert percentile([0.0, 10.0, 20.0], 0.5) == 10.0

    frame = synthetic_frame(0.0)
    assert len(frame) == PIXEL_COUNT
    assert all(isinstance(value, float) for value in frame)
    assert 15.0 < center_temperature(frame) < 50.0

    fixed = TemperatureScale(20.0, 40.0)
    assert fixed.update(frame) == (20.0, 40.0)
    automatic = TemperatureScale(None, None)
    low, high = automatic.update(frame)
    assert high - low >= 5.0
    print("All core tests passed.")


if __name__ == "__main__":
    run()
