from keel.factors.kinematics import calculate_calculus, calculate_multi_timeframe


def test_calculus_is_causal_and_bounded():
    base = [100, 101, 102, 104, 107, 111, 116, 122, 129, 137]
    a = calculate_calculus(base)
    b = calculate_calculus(base + [145])
    assert a["valid"] and b["valid"]
    assert -3 <= a["velocity"] <= 3
    assert -3 <= a["acceleration"] <= 3
    assert -3 <= a["impulse"] <= 3
    assert a["velocity"] != b["velocity"]


def test_multi_timeframe_reverses_okx_newest_first():
    rows = [[str(i), "101", "99", str(100 + i), "1"] for i in range(10)]
    result = calculate_multi_timeframe({"15M": list(reversed(rows))})
    assert result["valid"]
    assert result["timeframes"]["15M"]["velocity"] > 0
