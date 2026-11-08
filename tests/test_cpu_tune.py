from headroom import cpu_tune


def test_kill_script_never_matches_everything():
    for cc in ("", None, "C:\\", "relative\\dir", "C:"):
        s = cpu_tune.kill_script(cc)
        assert "ExecutablePath" not in s          # only the CoreCycler command-line match remains
        assert "script-corecycler" in s


def test_kill_script_exact_prefix_and_quotes():
    s = cpu_tune.kill_script("C:\\Program Files\\Headroom\\tools\\Core'Cycler[1]")
    assert "StartsWith('C:\\Program Files\\Headroom\\tools\\Core''Cycler[1]\\'" in s
    assert "-like" not in s


def test_final_values_margin_never_positive():
    assert cpu_tune.final_values([-30, -2, 0]) == [-27, 0, 0]
