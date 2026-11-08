from headroom import fan_curve as fc


def test_other_controllers_detects_glint():
    seen = fc.other_controllers(lambda *names: [n for n in names if n in ("glint.exe",)])
    assert seen == ["Glint"]
    assert fc.other_controllers(lambda *names: []) == []


def test_contest_judge_needs_sustained_mismatch():
    j = fc.ContestJudge()
    assert not j.check(60, 30, 0)
    assert not j.check(60, 30, fc.CONTEST_SEC - 1)
    assert j.check(60, 30, fc.CONTEST_SEC + 0.1)
    j2 = fc.ContestJudge()
    assert not j2.check(60, 30, 0)
    assert not j2.check(60, 58, 5)          # back in tolerance resets the timer
    assert not j2.check(60, 30, 6)
    assert not j2.check(60, 30, 6 + fc.CONTEST_SEC - 1)


def test_clean_curve_monotonic_and_clamped():
    pts = fc._clean([[90, 50], [40, 80], ["x", 1], [200, 120]], 20, 90)
    assert pts == [[40, 80], [90, 80], [90, 100]]


def test_target_pct_emergency_and_unreadable():
    cfg = fc.load_config()
    assert fc.target_pct(cfg, None, None) == (100, "core temp unreadable")
    assert fc.target_pct(cfg, fc.EMERG_CORE, None)[0] == 100
    assert fc.target_pct(cfg, 30, 50)[0] == 0
